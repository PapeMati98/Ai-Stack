# Proactive Telegram notices

Load when judging an unsolicited notice, once per episode. Use the existing
Office dispatcher and event IDs; do not create another queue, scheduler or
model polling loop. Triage suggests a disposition; it consumes nothing and
does not claim delivery or product acceptance.

## What earns a notice

Send a useful completed outcome, an exact owner decision, material blockage,
deadline risk or budget/availability change that actually affects the task.
Say what changed and the next useful action. Preserve the evidence time and
state uncertainty plainly. Do not push every tool result, read, ACK or presence.
Routine progress belongs in a digest or the same status card.

The configured bot's authorized Office scope covers routine replies and approved
proactive events. Do not require fresh consent or a process receipt for every
notification. Bot activation, a new paid cost, access or production effect still
need their appropriate owner authorization; triage never grants it.

Daily briefings need an actual authorized schedule. Invoking this skill creates
none. Waiting runs without a model. The helper's conservative 22..7 quiet default
is a draft policy, not a deployed schedule; the bridge uses owner preferences
and computes the local hour in the configured timezone.

## Order and reasons

1. unknown_outcome -> defer/reconcile_unknown, even without a confirmed event ID.
   Reconcile transport before sending or editing. Never send a reconciliation
   notice as a way around this guard.
2. Same confirmed event ID at equal/newer revision -> suppress/duplicate_confirmed.
3. Expired decision window -> suppress/expired. Compare zoned timestamps, not
   their text order. Composer renders an expired decision without active buttons.
4. Briefing without configured schedule -> suppress/no_schedule.
5. Mute -> suppress/muted unless the exact event ID+revision has a configured
   critical exception. Caller-labelled severity alone cannot override preferences.
6. Routine kinds (progress/ack/presence/chatter) -> digest/routine_digest regardless
   of an inflated severity label.
7. Quiet hours -> defer/quiet_hours unless that same explicit exception applies.
8. New revision of a confirmed material event -> edit/update_card only with a
   confirmed message ID. Without it -> defer/missing_card_ref; do not invent send.
9. A configured critical event -> send/critical_exception. Other material events
   or an authorized briefing -> send/material_event. Everything else -> digest.

An override never defeats unknown, dedupe, expiry or the briefing schedule.
Preferences and prior receipts come from bridge state, never from untrusted
event text. An override is exact, not an unrestricted nightly bypass.

## JSON schema

    {"operation":"triage",
     "event":{"id":"result_7","revision":1,"kind":"outcome","severity":"material"},
     "now":{"local_hour":12,"iso":"2026-10-07T12:00:00Z"},
     "preferences":{"quiet_start":22,"quiet_end":7,"muted":false,
                    "briefing_configured":false,"critical_exceptions":[]},
     "prior":{}}

event: nonempty id, nonnegative integer revision; kind is
outcome/decision/blockage/budget/deadline/progress/ack/presence/chatter/briefing.
severity is routine/material/critical (default routine). Optional expires_at
requires now.iso with timezone. now.local_hour is a caller-computed integer 0..23;
the helper does not read a clock. Quiet spans are start-inclusive/end-exclusive;
equal endpoints disable quiet hours, overnight spans wrap correctly.

prior: confirmed_event_id?, confirmed_revision? (default -1),
confirmed_message_id? (positive integer), unknown_outcome? (default false).
critical_exceptions: owner-configured [{"event_id":"alert_7","revision":2}].
It applies only to critical material events. It does not create approval or
authorize the underlying operation.

Output: {"ok":true,"operation":"triage","result":{"action":
"send|edit|defer|digest|suppress","reason":"one reason above","detail":"..."}}.
The current bridge records a receipt only after transport confirmation. Details
and reason codes are for operator diagnostics; the owner sees a useful sentence,
not an internal policy checklist. Malformed inputs exit 1 with an error object.

## Forward examples

- Claude finished a report: one result card with accepted vs pending review
  stated accurately; send in the allowed window, otherwise defer.
- Agent needs a package approval: precise revision and target, recommendation
  and consequences, scoped buttons; settlement edits that same card.
- Brainstorming: one consultative poll, never production approval.
- Routine progress or duplicate ACK: digest/suppress, no new owner interruption.
- Prior send unknown: reconcile existing state, never blind repeat or fallback.
