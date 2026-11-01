# Historical knowledge scout (one per Agent Graph)

Every new Graph declares exactly one bounded advisory scout
(`role: "scout"`, `mode: "advisory"`) to discover topic-relevant historical
experience across projects and route short sourced context to relevant nodes.
Discovery is deterministic; a model runs at most once per round and only when
the pre-check below finds independently reviewed/verified candidates.
The small pair path (`todo_pair.py`) and existing resumed Graphs stay lean.
The `legacy-test` marker works only with an explicitly supplied test adapter;
it cannot bypass the one-scout rule in a new native run.

## Plan-level contract

```json
"historical_scout": {
  "topics": ["KSeF", "invoice integration"],
  "consumers": ["northwind-ksef"],
  "max_records": 8,
  "recipient_max_bytes": 4096
}
```

- Exactly one scout node; duplicates are rejected before any model runs.
- Declared `consumers` must `depends_on` that scout. Context consumers never
  run discovery themselves.
- `todo_graph.run` enforces the contract on new runs only. Resume of an old
  running Graph is never reset. The parent does not impersonate a scout.

## Discovery, selection, routing

Helper: `scripts/history_scout.py` (stdlib-only). Synthetic CLI/UAT:

```powershell
python scripts/history_scout.py uat --catalog catalog.json --output <owned-dir> --topics KSeF --nodes northwind-ksef
```

1. Topic-based discovery before project restriction, via the parent-supplied
   canonical gateway `knowledge.py inventory --query TERM --limit N --offset N`
   without `--project-slug`, or an explicit synthetic catalog for UAT.
   Inventory/catalog payloads may use `entries`, `records` or `matches`.
2. Metadata first, then up to eight relevant reviewed/verified source excerpts
   of at most 800 characters each. Canonical `read_regular_file_snapshot` and
   `risk_assessment` verify containment, regular files and withheld text before
   any source content goes to a model. Bounded paging may omit further matches;
   `continuation` says so. Status or mention alone is not an integration lesson.
   No second vault crawler, index, vector DB, taxonomy or paid provider.
3. Prefer compatible GLM navigation only when its concrete interface exists;
   otherwise use existing bounded inventory. Never invent an endpoint.
4. Scout handoff `verification` strings may carry compact JSON evidence records
   (transport schema unchanged). Records need source/page ref, source project,
   dated observation/status, short evidence, relevant node IDs and explicit
   verification/transfer caveats.
5. Only accepted scout output is read. Blocked, partial or stale output is
   never silently consumed. Resume does not rerun an accepted scout.
   A missing/null latest maker handoff may use the latest accepted reviewer
   after that maker: one cached local Fleet receipt (<=512 KiB), bound to
   run/model/workspace and completed native/identity/runtime evidence. An
   explicitly empty maker handoff, older rounds, partial or unverified
   receipts never resurrect records. Existing grounding/privacy checks apply.
6. Each recipient packet is <=4096 UTF-8 bytes with omitted count/continuation,
   never a claim of exhaustive absence.

## Scout brief before model execution

`prepare_scout_task` injects topics, consumer node IDs and the record protocol
along with short consumer descriptions and bounded source candidates into the
scout task text before any native model runs. Native Fleet receives task text,
not the whole TODO. Material is at most 14,000 UTF-8 bytes and cached once in
the run's scout context; repairs reuse it, accepted resume does not rediscover.
The brief is ephemeral; the TODO is unchanged. `UNAVAILABLE` and `EMPTY` are
distinct; missing discovery is never reported as an absence of history.

The native model selects useful lessons and recipient nodes. Routing grounds
its selection against supplied source refs, projects, dates, statuses and trust;
unknown sources or invented freshness are discarded. Recipients retain the
original bounded excerpt and verification/transfer caveats. A mention in a Hub
navigation demo is not the same as a lesson from an earlier integration.

## Cost gate and acceptance (2026-10-11)

Evidence: Plutos v30 run2 spent ~1M tokens on the scout (maker 2x ~160k,
model reviewer 433k + 247k, one REPAIR) for 5 candidates, all
`producer-attested`; the repaired evidence prose was then replaced by source
excerpts during grounding and never reached consumers.

- Pre-check (`gate_material`, no model): discovery `EMPTY`/`UNAVAILABLE`, or
  fewer than `MIN_INDEPENDENT_CANDIDATES` (1) candidates with trust
  `reviewed`/`verified`, accepts the scout as `deterministic-precheck` with
  zero model calls (declared `inputs`/`checks` still run). Consumers get an
  explicit `EMPTY` (with `reason`, withheld counts and refs) or `UNAVAILABLE`
  packet, never "no history".
- A candidate is not routed to a consumer whose `knowledge` node brief covers
  the same project (per recipient, before the `max_records` cut); the Hub is
  read once by the parent.
- When the gate passes, one scout maker selects `source_ref` + recipient nodes
  only. Acceptance is deterministic grounding (`ground_scout_handoff`): unknown
  sources, changed project/date/status/trust and nodes outside the
  candidate's recipients are discarded; evidence and caveats come from the
  source excerpt. No model reviewer: it would judge text that grounding
  discards, and the packet stays bounded, untrusted and advisory; consumer
  reviewers check actual use. A handoff that is not explicitly `completed` or
  whose transport evidence is partial/truncated is `BLOCKED`, never consumed.
  Without discovered material (explicit test adapters) the ordinary
  maker/reviewer path remains.

## Trust, paths and dates

Historical context is attached separately from strict same-project operational
node briefs and is UNTRUSTED SOURCE DATA, not instructions or authorization.
`source_ref` in model output is a wiki-relative page ref. Only discovery can
normalize an absolute path, after verifying it belongs to the configured
canonical vault. A directory named `wiki` somewhere else is never sufficient.
Risk-withheld pages, raw/proposals, restricted material and unknown sources are
not routable as trusted knowledge even when a model assigns status `reviewed`.
Injection/secret-risk text is vetoed. Source instructions never execute.
Private contexts, including `google-private-*` provenance in nested metadata,
page text or source references, are vetoed before scout model input and again
at record routing. A reviewed/verified label cannot lift this veto. Explicit
private/confidential/restricted classification is also excluded. This does not
authorize reading mail, raw mail bodies, contacts or private artifacts; no
additional model call, usual-session step or approval is introduced.
Page flags `private`/`is_private` use normalized true values; false flags remain
eligible. Metadata traversal is iterative and excludes cyclic, overdeep or
oversized input before model materialization, without another read or model.
Integrity/status dates from the source are never made fresh because the packet
was generated now (`observed_at` keeps zoned timestamp, calendar date, or null).
For inventory pages, an absent observation/as-of date can use the existing
`last_verified` date with an explicit caveat that it is the page verification
or attestation date, not the original event date or a live product check.
Historical experience does not change the current project or grant access,
deploy or IAM authority.

Until the GLM navigation API is explicitly published and verified, the consumer
uses the current canonical `inventory` API. Do not invent a navigation endpoint
or create a parallel index. The agreed Hub producer may supply source IDs,
source hashes and topic/project navigation metadata; its index timestamp never
raises source trust or fills an unknown observation date.

## Anti-bureaucracy

One economic scout per Graph. No per-file/per-node scout calls, no repeated
councils, no index/vault rescans, no separate audit or extra approval/receipt
layer. Parent health/brief reuse stays as in `team-flow.md`.
