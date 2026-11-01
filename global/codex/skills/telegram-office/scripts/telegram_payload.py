"""telegram-office payload helper: JSON stdin -> JSON stdout.

Pure offline builder for Telegram Bot API request payloads. It NEVER sends,
registers, schedules, reads credentials, touches the network, the filesystem
(other than stdio), or the environment. It only validates input and returns
native API request objects (sendMessage / editMessageText / sendPoll
parameters).

Usage:
    python scripts/telegram_payload.py < input.json > output.json

Envelope in (exactly one operation per call):
    {"operation": "compose", ...}   status/question/result/decision card or poll
    {"operation": "triage", ...}    send/edit/defer/digest/suppress suggestion

Envelope out, success (exit 0):
    {"ok": true, "operation": "compose", "result": {...}}
    {"ok": true, "operation": "triage", "result": {"action": ..., ...}}

Envelope out, failure (exit 1, fail closed):
    {"ok": false, "error": {"code": "...", "message": "..."}}

Full schema lives in references/interaction-playbook.md (compose) and
references/proactive-triage.md (triage).
"""

import html
import json
import re
import sys
from datetime import datetime

MAX_INPUT_BYTES = 65536
MAX_TEXT_CHARS = 4096        # sendMessage text limit after parsing
MAX_CALLBACK_BYTES = 64      # callback_data 1..64 bytes
MAX_QUESTION_CHARS = 300     # sendPoll question 1..300 chars
MAX_OPTION_CHARS = 100       # sendPoll option text 1..100 chars
MIN_POLL_OPTIONS = 2
MAX_POLL_OPTIONS = 12
MAX_BUTTONS = 8
MAX_STR_FIELD = 2000

AUTHORS = ("Codex", "Claude", "ZCode", "Hermes", "Antigravity")
CARD_KINDS = ("status", "question", "result", "decision")
# Methods permitted when the helper is invoked from the send-only cycle of
# telegram_service.py --send-only. The bridge may consume no inbound updates,
# no callback answers, no webhook mutations, so every Telegram method other
# than these two is rejected fail-closed with SEND_ONLY_DENIED.
SEND_ONLY_METHODS = frozenset({"sendMessage", "editMessageText"})
MATERIAL_KINDS = ("outcome", "decision", "blockage", "budget", "deadline")
ROUTINE_KINDS = ("progress", "ack", "presence", "chatter")
EVENT_KINDS = MATERIAL_KINDS + ROUTINE_KINDS + ("briefing",)


def fail(code, message):
    sys.stdout.write(json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.exit(1)


def ok(operation, result):
    sys.stdout.write(json.dumps({"ok": True, "operation": operation, "result": result}, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.exit(0)


def esc(text):
    return html.escape(text, quote=False)


def utf8_len(text):
    return len(text.encode("utf-8"))


def need_str(obj, field, max_len=MAX_STR_FIELD, required=True):
    val = obj.get(field)
    if val is None:
        if required:
            fail("MALFORMED", "missing required field: %s" % field)
        return None
    if not isinstance(val, str):
        fail("MALFORMED", "field %s must be a string" % field)
    if len(val) > max_len:
        fail("INPUT_TOO_LARGE", "field %s exceeds %d characters" % (field, max_len))
    return val


def check_chat_id(chat_id):
    # Office owner chats use numeric bridge-bound IDs, not public @channels.
    if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
        fail("AMBIGUOUS_ADDRESSING", "chat_id must be an exact numeric bridge-bound id")
    if isinstance(chat_id, str) and not re.fullmatch(r"-?[1-9][0-9]{0,15}", chat_id):
        fail("AMBIGUOUS_ADDRESSING", "chat_id must be numeric, never a title or username")
    numeric = int(chat_id)
    if numeric == 0 or abs(numeric) >= 2 ** 52:
        fail("AMBIGUOUS_ADDRESSING", "chat_id is outside the supported Telegram ID range")
    return numeric


def timestamp(value, field):
    if not isinstance(value, str):
        fail("MALFORMED", "%s must be an ISO timestamp with timezone" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail("MALFORMED", "%s must be an ISO timestamp with timezone" % field)
    if parsed.tzinfo is None:
        fail("MALFORMED", "%s must include a timezone offset" % field)
    return parsed


def decision_card(card, now_iso):
    """Validate a bridge-owned reference; this never authenticates approval."""
    if card.get("kind") != "decision":
        return card
    scope = card.get("decision")
    if not isinstance(scope, dict):
        fail("UNSCOPED_DECISION", "decision card needs a bridge-owned decision object")
    reference = need_str(scope, "scope_ref", max_len=64)
    check_callback_token(reference)
    slug = need_str(scope, "project_slug", max_len=100)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        fail("UNSCOPED_DECISION", "project_slug must be a canonical Hub slug")
    operation = need_str(scope, "operation", max_len=1000)
    if not operation.strip():
        fail("UNSCOPED_DECISION", "decision must describe the exact operation")
    revision = scope.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        fail("UNSCOPED_DECISION", "decision revision must be a non-negative integer")
    # The bridge resolves route_ref against the existing Office registry.
    # An opaque route avoids guessing native IDs from labels in the helper.
    check_callback_token(need_str(scope, "route_ref", max_len=64))
    expires = timestamp(scope.get("expires_at"), "decision.expires_at")
    current = timestamp(now_iso, "now_iso")
    result = dict(card)
    if card.get("project") is not None and card["project"] != slug:
        fail("UNSCOPED_DECISION", "card project differs from scoped project_slug")
    result["project"] = slug
    result["decision_context"] = "%s (rev %d)" % (operation, revision)
    result["decision_expiry"] = scope["expires_at"]
    if current >= expires:
        result.update(state="settled", buttons=[], force_reply=False)
        result["decision_expired"] = True
    return result


def check_callback_token(token):
    if not isinstance(token, str):
        fail("INVALID_CALLBACK", "callback_data must be a string token")
    size = utf8_len(token)
    if size < 1 or size > MAX_CALLBACK_BYTES:
        fail("INVALID_CALLBACK", "callback_data must be 1..64 UTF-8 bytes, got %d" % size)
    if any((c.isspace() or ord(c) < 32) for c in token):
        fail("INVALID_CALLBACK", "callback_data must be an opaque token without whitespace")
    if token.startswith("/") or "://" in token:
        fail("INVALID_CALLBACK", "callback_data must be opaque, never a command or secret")
    return token


def check_url(url):
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > MAX_STR_FIELD:
        fail("UNSAFE_URL", "URL buttons must use an explicit https:// address")
    if any((ord(c) < 32) for c in url):
        fail("UNSAFE_URL", "URL button address contains control characters")
    return url


def build_card_text(card, parse_mode):
    author = card.get("author")
    if author not in AUTHORS:
        fail("MALFORMED", "author must be one of %s" % ",".join(AUTHORS))
    kind = card.get("kind")
    if kind not in CARD_KINDS:
        fail("MALFORMED", "card kind must be status/question/result/decision")
    title = need_str(card, "title", required=False) or ""
    body = need_str(card, "body", max_len=20000)
    project = need_str(card, "project", required=False)
    stage = need_str(card, "stage", required=False)
    updated = need_str(card, "updated_at", required=False)

    if kind == "decision":
        recommendation = need_str(card, "recommendation")
    else:
        recommendation = need_str(card, "recommendation", required=False)

    consequences = need_str(card, "consequences", required=False)
    state = card.get("state", "active")
    if state not in ("active", "settled"):
        fail("MALFORMED", "state must be active or settled")

    buttons = card.get("buttons", [])
    force_reply = card.get("force_reply", False)
    if not isinstance(force_reply, bool):
        fail("MALFORMED", "force_reply must be a boolean")
    if not isinstance(buttons, list):
        fail("MALFORMED", "buttons must be a list")
    if len(buttons) > MAX_BUTTONS:
        fail("MALFORMED", "at most %d buttons per card" % MAX_BUTTONS)
    if buttons and force_reply:
        fail("MALFORMED", "choose buttons or ForceReply text, never both")
    if state == "settled" and buttons:
        fail("SETTLED_NO_BUTTONS", "settled cards must drop obsolete buttons, not keep them")
    if state == "settled" and force_reply:
        fail("SETTLED_NO_BUTTONS", "settled cards must not request new input")

    markup = None
    if buttons:
        row = []
        for btn in buttons:
            if not isinstance(btn, dict):
                fail("MALFORMED", "each button must be an object")
            text = need_str(btn, "text", max_len=200)
            if not text.strip():
                fail("MALFORMED", "button text must not be blank")
            has_cb = "callback_data" in btn
            has_url = "url" in btn
            if has_cb == has_url:
                fail("MALFORMED", "each button carries exactly one action: callback_data or url")
            if has_cb:
                row.append({"text": text, "callback_data": check_callback_token(btn["callback_data"])})
            else:
                row.append({"text": text, "url": check_url(btn["url"])})
        markup = {"inline_keyboard": [[b] for b in row]}
    elif force_reply:
        markup = {"force_reply": True}

    if parse_mode == "HTML":
        head = esc("[%s] %s" % (author, title)) if title else esc("[%s]" % author)
        lines = ["<b>%s</b>" % head, esc(body)]
        if project:
            lines.append(esc("Projekt: %s" % project))
        if stage:
            lines.append(esc("Etap: %s" % stage))
        if updated:
            lines.append(esc("Aktualizacja: %s" % updated))
        if recommendation:
            lines.append("<b>Rekomendacja:</b> %s" % esc(recommendation))
        if consequences:
            lines.append("<b>Skutki:</b> %s" % esc(consequences))
        if card.get("decision_context"):
            lines.append(esc("Operacja: %s" % card["decision_context"]))
            lines.append(esc("Wazne do: %s" % card["decision_expiry"]))
        if state == "settled":
            lines.append("<b>Status:</b> %s" % ("wygaslo, odpowiedzi zamkniete" if card.get("decision_expired") else "rozstrzygnieto, odpowiedzi zamkniete"))
        text = "\n".join(lines)
    else:
        head = "[%s] %s" % (author, title) if title else "[%s]" % author
        lines = [head, body]
        if project:
            lines.append("Projekt: %s" % project)
        if stage:
            lines.append("Etap: %s" % stage)
        if updated:
            lines.append("Aktualizacja: %s" % updated)
        if recommendation:
            lines.append("Rekomendacja: %s" % recommendation)
        if consequences:
            lines.append("Skutki: %s" % consequences)
        if card.get("decision_context"):
            lines.append("Operacja: %s" % card["decision_context"])
            lines.append("Wazne do: %s" % card["decision_expiry"])
        if state == "settled":
            lines.append("Status: %s" % ("wygaslo, odpowiedzi zamkniete" if card.get("decision_expired") else "rozstrzygnieto, odpowiedzi zamkniete"))
        text = "\n".join(lines)

    if len(text) > MAX_TEXT_CHARS:
        fail("TEXT_TOO_LONG", "card exceeds the conservative 4096-character encoded-text bound")
    return text, markup


def do_compose(req):
    # Defense-in-depth: a send-only consumer may only push cards via the two
    # production-safe methods. Buttons or any other action is incompatible with
    # a relay that owns no inbound update channel.
    send_only = req.get("send_only", False) is True
    if send_only:
        card = req.get("card") if isinstance(req.get("card"), dict) else None
        if card is not None and isinstance(card.get("buttons"), list) and card["buttons"]:
            fail("SEND_ONLY_NO_BUTTONS",
                 "send-only cycle refuses cards that carry active buttons")
    chat_id = check_chat_id(req.get("chat_id"))
    thread_id = req.get("message_thread_id")
    if thread_id is not None and (isinstance(thread_id, bool) or not isinstance(thread_id, int) or thread_id <= 0):
        fail("MALFORMED", "message_thread_id must be a positive int topic id when present")
    reply_to = req.get("reply_to_message_id")
    if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to <= 0):
        fail("MALFORMED", "reply_to_message_id must be a positive int when present")
    edit_id = req.get("edit_message_id")
    if edit_id is not None and (isinstance(edit_id, bool) or not isinstance(edit_id, int) or edit_id <= 0):
        fail("MALFORMED", "edit_message_id must be a positive int bridge message id when present")
    parse_mode = req.get("parse_mode", "HTML")
    if parse_mode not in ("HTML", "none"):
        fail("MALFORMED", "parse_mode must be HTML or none")
    fmt = req.get("format", "card")

    params = {"chat_id": chat_id}
    if thread_id is not None:
        params["message_thread_id"] = thread_id
    if reply_to is not None:
        params["reply_parameters"] = {"message_id": reply_to}

    if fmt == "card":
        card = req.get("card")
        if not isinstance(card, dict):
            fail("MALFORMED", "compose card requires a card object")
        card = decision_card(card, req.get("now_iso"))
        text, markup = build_card_text(card, parse_mode)
        if edit_id is not None:
            if card.get("force_reply"):
                fail("MALFORMED", "editMessageText supports inline keyboards, not ForceReply")
            edit_params = {"chat_id": chat_id, "message_id": edit_id, "text": text}
            if parse_mode == "HTML":
                edit_params["parse_mode"] = "HTML"
            # Explicitly clear a former keyboard on the same card.
            edit_params["reply_markup"] = markup or {"inline_keyboard": []}
            result = {"requests": [{"method": "editMessageText", "params": edit_params}],
                      "meta": {"format": "card", "kind": card.get("kind"), "edit": True}}
            if send_only:
                _enforce_send_only(result)
            return ok("compose", result)
        params["text"] = text
        if parse_mode == "HTML":
            params["parse_mode"] = "HTML"
        if markup is not None:
            params["reply_markup"] = markup
        result = {"requests": [{"method": "sendMessage", "params": params}],
                  "meta": {"format": "card", "kind": card.get("kind")}}
        if send_only:
            _enforce_send_only(result)
        return ok("compose", result)
    elif fmt == "poll":
        if edit_id is not None:
            fail("MALFORMED", "polls cannot reuse edit_message_id; sent polls are immutable text")
        poll = req.get("poll")
        if not isinstance(poll, dict):
            fail("MALFORMED", "compose poll requires a poll object")
        author = poll.get("author")
        if author not in AUTHORS:
            fail("MALFORMED",
                 "author must be one of %s" % ",".join(AUTHORS))
        question = need_str(poll, "question", max_len=MAX_QUESTION_CHARS)
        question = "[%s] %s" % (author, question)
        if not (1 <= len(question) <= MAX_QUESTION_CHARS):
            fail("MALFORMED", "poll question including author must be 1..300 characters")
        options = poll.get("options")
        if not isinstance(options, list) or not (MIN_POLL_OPTIONS <= len(options) <= MAX_POLL_OPTIONS):
            fail("MALFORMED", "Office consultative poll needs 2..12 options")
        clean_options = []
        for opt in options:
            if not isinstance(opt, str) or not (1 <= len(opt) <= MAX_OPTION_CHARS):
                fail("MALFORMED", "each poll option must be 1..100 characters")
            clean_options.append({"text": opt})
        poll_params = dict(params)
        poll_params["question"] = question
        poll_params["options"] = clean_options
        anonymous = poll.get("is_anonymous", True)
        if not isinstance(anonymous, bool):
            fail("MALFORMED", "is_anonymous must be a boolean")
        poll_params["is_anonymous"] = anonymous
        if poll.get("allows_multiple_answers") is True:
            poll_params["allows_multiple_answers"] = True
        requests = []
        preface = poll.get("preface")
        if preface is not None:
            if not isinstance(preface, str) or not preface or len(preface) > MAX_TEXT_CHARS:
                fail("MALFORMED", "poll preface must be 1..4096 characters")
            pre = dict(params)
            pre["text"] = "[%s] %s" % (author, preface) if parse_mode == "none" else "<b>%s</b>\n%s" % (esc("[%s]" % author), esc(preface))
            if parse_mode == "HTML":
                pre["parse_mode"] = "HTML"
            if len(pre["text"]) > MAX_TEXT_CHARS:
                fail("TEXT_TOO_LONG", "poll preface including author exceeds the encoded-text bound")
            requests.append({"method": "sendMessage", "params": pre})
        requests.append({"method": "sendPoll", "params": poll_params})
        result = {"requests": requests,
                  "meta": {"format": "poll", "consultative_only": True,
                           "note": "Poll votes are consultative brainstorming input, never production consent."}}
        if send_only:
            _enforce_send_only(result)
        return ok("compose", result)
    else:
        fail("MALFORMED", 'format must be "card" or "poll"')


def _enforce_send_only(result):
    """A send-only consumer may only emit sendMessage/editMessageText.

    Polls, stickered actions and any unknown method are rejected fail-closed
    with SEND_ONLY_DENIED. Anything beyond these two methods would implicitly
    require an inbound channel that send-only does not own.
    """
    requests = result.get("requests") if isinstance(result, dict) else None
    if not isinstance(requests, list):
        fail("MALFORMED", "compose result must carry a requests list")
    for req in requests:
        method = req.get("method") if isinstance(req, dict) else None
        if method not in SEND_ONLY_METHODS:
            fail("SEND_ONLY_DENIED",
                 "send-only cycle permits only sendMessage/editMessageText, not %s" % method)


def in_quiet(hour, start, end):
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def do_triage(req):
    event = req.get("event")
    if not isinstance(event, dict):
        fail("MALFORMED", "triage requires an event object")
    eid = event.get("id")
    rev = event.get("revision")
    kind = event.get("kind")
    if not isinstance(eid, str) or not eid:
        fail("MALFORMED", "event.id must be a non-empty string")
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        fail("MALFORMED", "event.revision must be a non-negative int")
    if kind not in EVENT_KINDS:
        fail("MALFORMED", "event.kind must be one of: %s" % ",".join(EVENT_KINDS))
    severity = event.get("severity", "routine")
    if severity not in ("routine", "material", "critical"):
        fail("MALFORMED", "severity must be routine/material/critical")

    now = req.get("now")
    if not isinstance(now, dict):
        fail("MALFORMED", "triage requires a now object with caller local_hour")
    hour = now.get("local_hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or not (0 <= hour <= 23):
        fail("MALFORMED", "now.local_hour must be caller-provided 0..23")
    now_iso = now.get("iso")

    prefs = req.get("preferences", {})
    if not isinstance(prefs, dict):
        fail("MALFORMED", "preferences must be an object")
    qstart = prefs.get("quiet_start", 22)
    qend = prefs.get("quiet_end", 7)
    for v in (qstart, qend):
        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 23):
            fail("MALFORMED", "quiet_start/quiet_end must be 0..23")
    muted = prefs.get("muted", False) is True
    for flag in ("muted", "briefing_configured"):
        if flag in prefs and not isinstance(prefs[flag], bool):
            fail("MALFORMED", "%s must be a boolean" % flag)

    prior = req.get("prior", {})
    if not isinstance(prior, dict):
        fail("MALFORMED", "prior must be an object")
    confirmed_id = prior.get("confirmed_event_id")
    confirmed_rev = prior.get("confirmed_revision", -1)
    unknown = prior.get("unknown_outcome", False) is True
    if "unknown_outcome" in prior and not isinstance(prior["unknown_outcome"], bool):
        fail("MALFORMED", "unknown_outcome must be a boolean")
    if isinstance(confirmed_rev, bool) or not isinstance(confirmed_rev, int) or confirmed_rev < -1:
        fail("MALFORMED", "confirmed_revision must be an integer >= -1")

    # Reconciliation and dedupe outrank every send verdict, including
    # caller-marked critical events: severity is caller-supplied and must
    # not defeat anti-spam or unknown-outcome reconciliation.
    if unknown:
        return ok("triage", {"action": "defer", "reason": "reconcile_unknown",
                             "detail": "A prior transport outcome is unknown; reconcile before any send or edit."})
    if isinstance(confirmed_id, str) and confirmed_id == eid:
        if isinstance(confirmed_rev, int) and confirmed_rev >= rev:
            return ok("triage", {"action": "suppress", "reason": "duplicate_confirmed",
                                 "detail": "Same event id+revision already confirmed delivered."})

    expires = event.get("expires_at")
    if expires is not None:
        if timestamp(now_iso, "now.iso") >= timestamp(expires, "event.expires_at"):
            return ok("triage", {"action": "suppress", "reason": "expired",
                                 "detail": "Decision window passed; render settled UI without buttons."})

    if kind == "briefing" and prefs.get("briefing_configured") is not True:
        return ok("triage", {"action": "suppress", "reason": "no_schedule",
                             "detail": "No authorized briefing schedule; invocation alone creates none."})

    overrides = prefs.get("critical_exceptions", [])
    if not isinstance(overrides, list) or any(not isinstance(item, dict) for item in overrides):
        fail("MALFORMED", "critical_exceptions must be scoped event id/revision records")
    critical_allowed = severity == "critical" and kind in MATERIAL_KINDS and any(
        item.get("event_id") == eid and type(item.get("revision")) is int and item["revision"] == rev
        for item in overrides)

    if muted and not critical_allowed:
        return ok("triage", {"action": "suppress", "reason": "muted",
                             "detail": "Owner muted proactive notices; non-critical event held."})

    if kind in ROUTINE_KINDS:
        return ok("triage", {"action": "digest", "reason": "routine_digest",
                             "detail": "Routine chatter folds into digest or updates the same card."})

    if in_quiet(hour, qstart, qend) and not critical_allowed:
        return ok("triage", {"action": "defer", "reason": "quiet_hours",
                             "detail": "Inside quiet hours; hold non-critical notice for delivery window."})

    if isinstance(confirmed_id, str) and confirmed_id == eid and kind in MATERIAL_KINDS:
        message_id = prior.get("confirmed_message_id")
        if type(message_id) is not int or message_id <= 0:
            return ok("triage", {"action": "defer", "reason": "missing_card_ref",
                                 "detail": "Need confirmed message_id to edit the same card; never invent a replacement send."})
        return ok("triage", {"action": "edit", "reason": "update_card",
                             "detail": "New revision of a delivered event; edit the same status card."})

    if critical_allowed:
        return ok("triage", {"action": "send", "reason": "critical_exception",
                             "detail": "Exact event id/revision is configured to override quiet hours and mute."})

    if kind in MATERIAL_KINDS or kind == "briefing":
        return ok("triage", {"action": "send", "reason": "material_event",
                             "detail": "Delivered outcome, owner decision, blockage, or budget/deadline impact."})

    return ok("triage", {"action": "digest", "reason": "routine_digest",
                         "detail": "Nothing material to push; keep it in the digest."})


def main():
    # JSON over Windows pipes must stay UTF-8, independent of console code page.
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except Exception:
        fail("MALFORMED", "cannot read stdin")
    if len(raw) > MAX_INPUT_BYTES:
        fail("INPUT_TOO_LARGE", "input exceeds 65536 bytes")
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception:
        fail("MALFORMED", "stdin must be a JSON object")
    if not isinstance(req, dict):
        fail("MALFORMED", "top-level JSON must be an object")
    op = req.get("operation")
    if op == "compose":
        do_compose(req)
    elif op == "triage":
        do_triage(req)
    else:
        fail("MALFORMED", 'operation must be "compose" or "triage"')


if __name__ == "__main__":
    main()
