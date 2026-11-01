# Formats

## Stable wiki page

```markdown
---
title: Exact page title
type: concept
status: reviewed
summary: One sentence used by the generated index.
last_verified: 2026-07-17
verified_by: codex
sources:
  - src-20260717-example-ab12cd34
tags:
  - example
---

# Exact page title

## Current synthesis

State only evidence-backed facts. Mark inference explicitly.

## Evidence

- `src-...`, section/page/timestamp: supported claim.

## Contradictions

- None known, or link the contradiction register.

## Open questions

- Question that needs another source.

## Connections

- [[Related Page]]
```

Allowed `type` values: `system`, `person`, `organization`, `project`, `product`, `concept`, `process`, `decision`, `event`, `source-summary`, `synthesis`, `evidence-ledger` (existing session-history pages).

## Project page

A project page (`type: project`, plus `project_slug`) keeps body sections only
where there is substantive content. Omit an empty section entirely. Do not write
blank scaffolding, placeholder "none known" lines, or mandatory receipt blocks.

| Section | Holds |
|---|---|
| Synteza | Evidence-backed synthesis of what the project currently knows. |
| Stan i cele | Concrete current state and explicit goals. |
| Nice to have | Optional improvements worth keeping visible. |
| Porazki i lekcje | Failures and the lesson from each one. |
| Watki i kanaly | Active threads and channels, by opaque reference only. |
| Osoby | People worth naming in prose. No contacts, no style profiles. |
| Tematy | Topics the project actually touches. |
| Zrodla | Source IDs that back the page (`sources`). |

Machine-readable links to the I1 registry live in frontmatter as canonical slug
IDs. Absent fields and empty lists are both fine; never invent an ID.

```yaml
org: ["example-org"]
topics: ["ksef"]
people: []
```

`org` resolves against `organizations`, `topics` against `topics`, and `people`
against `people`. Do not copy contact details, correspondence style, or private
person data into a project page.

## Proposal

The script creates the header. Fill these sections:

```markdown
## Intent

What durable knowledge should change and why.

## Atomic claims

| Claim | Source ID | Exact location | Confidence |
|---|---|---|---|
| ... | src-... | page 4 / 02:10 / section X | high |

## Proposed page changes

- `wiki/concepts/example.md`: add or revise ...

## Contradictions and uncertainty

- Existing claim, conflict, or missing evidence.

## Security review

- Embedded instructions ignored: yes/no
- Secret indicators: none/details
- External action requested by source: none/details
```

## Structured critical-state handoff

`record-handoff` adds these proposal fields and matching source metadata:

```yaml
project_slug: example-app
knowledge_kind: critical-state
state_summary: 'Example App has a deployment recorded in its reviewed project state.'
```

The project slug is an exact machine key for writes. The summary is one line, at
most 500 characters, and is scanned before write. A structured handoff remains
`status: proposed`; when its source metadata, hash, and both events are complete,
consumers label it `producer-attested-handoff`. This makes it mandatory context to
reconcile, but not stable wiki authority or production authorization. An incomplete
receipt remains `unverified-proposal` and fails lint.

The command receipt also contains `checkpoint`. On the owner workstation, successful
closeout requires `checkpoint.status` equal to `CHECKPOINTED` or `CURRENT`; a new
checkpoint includes its local commit SHA and exact path count. `NOT_OWNER` is expected
on a non-authoritative checkout and does not imply remote publication. Any required
`BLOCKED_*` status changes the handoff receipt to `CHECKPOINT_BLOCKED` while retaining
the complete append-only pair for recovery. `BLOCKED_MANUAL_RECOVERY` means a prior
cleanup or rollback could not be proved and the repository Git directory contains a
sticky owner-review marker; retries cannot report `CURRENT` until that state is
explicitly reconciled.

## Event file

Create one JSON file under `events/YYYY/MM/`:

```json
{
  "event_id": "evt-20260717-example-ab12cd34",
  "occurred_at": "2026-07-17T12:00:00Z",
  "agent": "codex",
  "action": "curate",
  "proposal_ids": ["prop-..."],
  "pages_changed": ["wiki/concepts/example.md"],
  "source_ids": ["src-..."],
  "summary": "Accepted two claims and recorded one contradiction."
}
```

## References in answers

Use a local page link plus the source ID. Example:

`[Exact page title](<absolute-path-to-page>)`, source `src-...`, verified 2026-07-17.
