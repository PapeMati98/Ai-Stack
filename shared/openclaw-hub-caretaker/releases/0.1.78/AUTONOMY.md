# Autonomous Hub caretaker — release 0.1.6

Owner decision: 10 consolidated ready groups OR age 24 hours for any nonempty
eligible backlog. An empty backlog makes no model calls. The age is checked on
each scheduled tick; it is not a promise that all accumulated work completes in
24 hours. The configured Windows task runs every 5 minutes and at user logon.
The activation receipt, not this document, proves installation.

Configuration: `<configured-caretaker-home>/control.json`,
fields `publication.min_ready_groups`, `publication.max_wait_hours`,
`publication.max_batch`. Defaults are 10 / 24 / 8 physical proposals per project
transaction. The last field is a transaction size, not the global audit trigger.

The owner Windows process reads the existing Hub and persists a frozen audit
cycle in `intake.sqlite`. Local official Qwen synthesizes a candidate. An actual
Agent CLI `todo_fleet.py` call invokes native Muse Spark1.3 Standard subscription
as an independent Meta reviewer. Only its accepted exact manifest may reach the
canonical owner publisher, curator lock and local checkpoint. No paid API or
Contributor fallback is enabled. WSL remains read-only.

One format repair and one semantic repair are bounded. A changed candidate
always gets a new independent review. Transport outages back off up to four
hours; deterministic publication failures quarantine unchanged content/code/
policy fingerprints. Project rotation prevents a large project monopolizing
ticks. A crashed run requires matching canonical journal, manifest, immutable
event and ledger hashes; missing proof remains explicitly UNKNOWN.

Sources and proposals are not deleted or rewritten. Grouping uses complete
producer+session provenance or exact source sets. Gateway session headers are
used only after original-byte integrity checks. A short envelope is not proof of
an orphan. Groups over eight originals and sources without a usable project are
reported as deferred/unsupported. They require a separately bounded handling
path; this release does not pretend every old report is publishable.

Observe `latest.json`, retained `runs/` and publication/audit tables. Model
unavailability and unassigned sources are visible, not converted to successes.
Native cross-tool coverage is limited to explicitly installed connectors and
configured source paths. Installed apps alone do not prove ingestion coverage.

The older README's initial-slice limitations describe historical development.
Current activation and live publication proofs are the owner deployment receipt
and canonical `governance/curations/evt-*.json` records. A successful knowledge
review does not prove live business-application production state.

Rollback: restore the prior scheduled task action/XML and backed-up control
configuration. Retain the SQLite database, prior releases, source evidence and
publication journals. Never reset the queue or remove an existing lock to force
success.

0.1.6 excludes unchanged quarantines from ready-count and age triggers; all original rows remain retained.
