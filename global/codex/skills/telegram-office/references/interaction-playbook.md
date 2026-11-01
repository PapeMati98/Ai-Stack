# Telegram interaction playbook

Read for composition, once per episode. Prefer a short useful answer with
details on demand over a catalogue of Telegram features.

## Choose the interaction

- Status/result: outcome first, then exact project, real stage and evidence
  timestamp. Distinguish queued, sent, started, received and accepted.
  Presence or ACK alone does not mean the task was done.
- Inline buttons: 2-4 useful choices, recommendation in text, one action per
  button. Use an opaque callback token for a bridge-owned record, never code,
  credentials or permission text. A URL may link approved HTTPS details.
- Native poll: brainstorming or preference collection. Office uses 2..12
  choices (API also allows a one-option poll). Votes and reactions never grant
  production permission, even with non-anonymous polls.
- ForceReply: request a free value such as a date or explanation. Do not
  combine it with inline buttons; it cannot be attached to editMessageText.
- Documents/details: give a compact answer first. Send an approved document
  or longer explanation on request, rather than many consecutive text chunks.
  Do not invent upload, transcription or rich-message tools.
- Voice: use only an existing approved transcription path. If unavailable,
  ask for text. Do not send audio to a new paid provider implicitly.

One Office bot represents the four logical authors in text: [Codex],
[Claude], [ZCode], [Antigravity]. These are labels, not four Telegram accounts.
Commands such as /status, /tasks, /decisions and /ask are useful when the
installed bridge implements them. Never pretend that an unimplemented command
or a stored Office message woke a Desktop.

## Addressing and correlation

Use numeric chat IDs bound by the bridge and exact topic/reply IDs.
Titles are display labels, never lookup keys. A project uses its Hub slug;
the bridge resolves the stored route to a strong Office/native session ID.
Private-chat topics require supported bot configuration. When unavailable,
omit message_thread_id and show the project in the card; do not guess a topic.

Keep one status card per event. Compose edit_message_id only from a confirmed
bridge receipt. Update the same card and explicitly clear obsolete buttons.
An unknown send outcome requires reconciliation, not an alternative send.
Topic identity for an edit is established by the original receipt; Telegram's
editMessageText identifies the card by chat_id + message_id.

## Owner decisions and callbacks

Show the precise operation, project, revision, recommendation and consequences.
The callback token refers to an immutable scoped bridge record, including the
allowed variants, intended recipient and validity window. The helper validates
a reference and draft; it does not authenticate the owner or authorize anything.

The bridge must verify callback_query.from against the owner allowlist, exact
chat/message, scope revision, expiry and already-settled state. A forged or stale
tap produces a short answerCallbackQuery notice and no side effect. Acknowledge
the callback promptly to clear the spinner; do not keep it pending during work.
Record a verified owner decision as a scoped Office event so relevant agents
can see it without importing chat history. A vote, label or forwarded text
is not that decision. Interpret ambiguous "yes" by its correlated card;
without correlation ask one concrete clarification.

After settlement, edit the same card with the selected variant and remove actions.
Expired cards say expired, not approved. No deletion of conversation history.

## Rendering and limits

Prefer escaped HTML or plain text. Never interpolate unescaped user/model text.
Keep paragraphs short; use on-demand detail instead of a long checklist.
sendMessage supports 4096 characters after parsing; this helper uses a stricter
4096-character encoded-text bound, including its own HTML tags. Callback tokens
are 1..64 UTF-8 bytes; byte limits matter for Polish characters.
sendPoll questions are 1..300 characters including the logical author label;
options are 1..100 characters each. Every poll question includes [Author].
Anonymity is explicit (true by default). All poll modes remain consultative.
Respect retry_after and chat rate limits through the existing dispatcher.
No automatic paid broadcasts, model polling, or blind retry after an unknown send.

Sources verified 2026-10-07:
[Bot API](https://core.telegram.org/bots/api),
[topics](https://core.telegram.org/bots/features#topics-in-private-chats),
[rate limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).

## Offline composer schema

JSON stdin:

    {"operation":"compose", "chat_id":123, "message_thread_id":4,
     "reply_to_message_id":90, "edit_message_id":55,
     "parse_mode":"HTML", "format":"card", "card":{...}}

Numeric chat_id is required; topic/reply/edit IDs are optional positive integers.
Numeric ID strings are normalized; names and usernames are rejected for Office
owner chats. parse_mode is HTML or none. format is card or poll.
reply_to_message_id is an input convenience; native output uses reply_parameters.
edit_message_id returns editMessageText instead of sendMessage; it rejects polls
and ForceReply. An edit without actions emits an empty inline keyboard.

Card: kind status/question/result/decision, author one of the four labels, body,
title?, project?, stage?, updated_at?, recommendation (required for decision),
consequences?, state active/settled, buttons? (max 8, each {text, callback_data}
or {text, url}), force_reply? (boolean). Settled cards reject new actions.

A decision additionally needs now_iso and this bridge-owned card.decision:

    {"scope_ref":"decision_42", "project_slug":"agent-stack",
     "operation":"Activate package abc in the isolated test profile",
     "revision":1, "route_ref":"office_route_7",
     "expires_at":"2026-10-07T22:00:00+02:00"}

Timestamps include timezone offsets. If now_iso reaches expiry, the composer
removes buttons/ForceReply and marks the card expired. project must match the
scoped slug. scope_ref and route_ref are opaque bridge references, not approval.

Poll: author, question, options (2..12 strings), is_anonymous? (default true),
allows_multiple_answers?, preface? (optional separate message). Omit preface for
one-message delivery. The bridge must rate-limit and reconcile each returned
request separately if a two-request poll draft is used.

Success: {"ok":true,"operation":"compose","result":{"requests":[
{"method":"sendMessage|editMessageText|sendPoll","params":{...}}],"meta":{...}}}.
Poll meta has consultative_only:true. The helper never sends anything.
Failure exits 1 with {"ok":false,"error":{"code":"...","message":"..."}}.
