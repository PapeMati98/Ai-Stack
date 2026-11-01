# Skill Evolution 0.1.1 contract

## Authority

The controller is an evidence and experiment ledger. It may create bounded
local JSON state only. It cannot create, edit, execute, install, synchronize,
deploy, promote, curate or release a skill, prompt, command, provider profile or
runtime. Every response and persisted decision carries no release authority.

## Trust boundary

Inputs are untrusted data. Only exact fields from the JSON schemas are accepted.
Free-form prose, prompts, code, diffs, executable content, secret-like strings,
relative paths, UNC paths and reparse-backed state roots fail closed. Source
artifacts remain outside the controller and are represented only by lowercase
SHA-256 plus bounded behavioral dimensions.

The local state root is an integrity ledger, not an authentication boundary.
Its hash chain detects malformed, partial and non-recomputed tampering, while
Windows ACLs protect the directory. An actor already able to rewrite every
state file as the same OS user can recompute an unkeyed chain; protecting
against that actor would require an external anchor or secret-bearing MAC and
is outside this shadow-only controller. Windows directory-entry durability
after an OS crash relies on `os.replace`; the no-op directory fsync on Windows
is an accepted availability residual, not release authority. Identifier
allowlists and exact schemas are the primary input boundary; marker lists are
defense in depth and do not replace structural validation.

## Behavior signature

A behavior observation is meaningful only in its complete context:

1. task family;
2. role;
3. model family;
4. runtime;
5. tool surface;
6. environment;
7. symptom;
8. expected behavior;
9. outcome.

The compiler never drops dimensions to manufacture a broader pattern. A group
requires two distinct source runs, two distinct actor identities and two
distinct provider lineages before it becomes eligible. A single compiled
pattern is therefore task-specific; a universal candidate must combine eligible
patterns from at least two model families and two runtimes.

## Paired verification

Each candidate is inert metadata plus the SHA-256 of material governed
elsewhere. The controller selects five targeted checks, three preservation
checks and five fresh confirmation checks. Baseline and candidate observations
must cite the same condition hash. The reviewer must differ from the proposer
in both actor identity and provider lineage.

Any attributable PASS-to-FAIL transition or preservation failure rejects the
candidate. A complete, independent pair with at least one attributable
FAIL-to-PASS recovery may produce `SHADOW_ACCEPTED`. Missing evidence produces
`NEEDS_EVIDENCE`; no-improvement produces `REJECTED`.

## Persistence

State uses canonical UTF-8 JSON, bounded collections, atomic replace, fsync,
cross-platform file locking and an append-only hash chain. Validation recomputes
the chain and binds every current collection back to its event payload before
every mutation and on explicit inspection. A candidate decision is terminal in
its run. `doctor`, `report` and `validate` perform zero writes.

The control plane is deliberately not a source of truth for shared skill
content. The shared origin, ownership manifest, transactional bootstrap, private
backup, native discovery and rollback remain separate mandatory controls.
