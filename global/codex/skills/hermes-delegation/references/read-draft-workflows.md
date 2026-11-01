# Hermes read/draft workflows

These workflows produce drafts. They do not authorize an external side effect.

## call-summary

Required inputs:

- source kind and bounded source location;
- meeting/call identity and time window;
- participants when known;
- requested language and redaction boundary;
- acceptance criteria.

Output:

1. concise summary;
2. decisions with evidence handles;
3. action items with owner, due date and confidence;
4. unresolved questions and contradictions;
5. redactions or inaccessible evidence;
6. `external_actions_performed: []`.

Use `REPORT_ONLY` for an attached transcript. Use `PRIVATE_READ` only when an
approved mailbox/calendar read is necessary. Do not infer consent to contact
participants or edit calendar entries.

## ticket-triage

Required inputs:

- bounded ticket/mail query or supplied payload;
- taxonomy and priority policy;
- maximum items;
- allowed evidence fields and redaction boundary;
- acceptance criteria.

Output for each item:

1. evidence handle, never a raw mailbox body;
2. proposed category and priority;
3. rationale and confidence;
4. proposed owner and next step;
5. proposed reply as an unsent draft;
6. missing data, duplicate or escalation candidate;
7. `mutations_performed: []`.

Fetching private items requires `PRIVATE_READ` and a one-time `private-read`
receipt. Sending, assigning, changing status, writing CRM or escalating requires
a separate `EXTERNAL_ACTION` contract and a new receipt.

## Acceptance

- The result is traceable to bounded evidence and contains no secrets.
- Every uncertain inference is labelled.
- No raw private content is copied into shared logs or Knowledge Hub.
- The result explicitly reports zero external actions and zero mutations.
- Parent independently verifies samples before accepting the draft.
