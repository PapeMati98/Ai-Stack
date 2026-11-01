# Agent Loop L2-thin contract

The L2 controller accepts only the exact schema in `l2-contract.schema.json`.
Unknown fields, duplicate JSON keys, linked inputs, dirty repositories, and
unfrozen actor assessments fail closed.

## Approval boundary

`prepare` validates all inputs, copies actor assessments into immutable loop
state, fingerprints the complete Agent Fleet runtime and controller, and emits
one canonical contract hash. `approve` accepts only that hash. A different
hash, altered assessment, changed runtime, moved controller, changed baseline,
or modified verifier invalidates the run.

## Actor boundary

The implementer and validator must be different Agent Fleet agents. Version
0.2.1 certifies only Codex and Claude for L2-thin. Other tools may discover and
orchestrate the skill but may not act as an L2 maker or reviewer.

## Verification boundary

The trusted runner must be a plain file below
`agent-loop/scripts/trusted_verifiers` in the sealed suite. Its exact SHA-256 is
frozen in the contract and rechecked before every state transition. It receives
only the candidate worktree path, a sanitized environment, and its declared
timeout. It must print exactly one JSON object with `status` equal to `PASS` or
`FAIL`; extra output fails closed.

`python_compile_v1.py` is the only generic verifier bundled in 0.2.1. It checks
tracked Python source for path safety, UTF-8 readability, size bounds, and
syntax compilation without importing or executing candidate modules. It is a
syntax gate, not a replacement for task-specific tests. A stronger verifier
must be audited and shipped in a later sealed suite release before the loop
contract can use it.

## Candidate boundary

Fleet creates the detached worktree. The controller requires the frozen HEAD,
rejects links and path escapes, enforces allowlisted and protected paths, caps
changed files and diff bytes, and fingerprints tracked plus untracked content.
The reviewer cannot be the maker. Reviewer PASS produces `CANDIDATE_READY`, not
acceptance and not integration.

## Terminal states

- `ACCEPTED`: parent verified and recorded acceptance; candidate remains
  detached until separately integrated or cleaned.
- `ESCALATED`: a failure needs parent judgment; automatic continuation is over.
- `STOPPED`: kill switch, budget, or operator stop ended the loop.

Only terminal loops can be cleaned. Cleanup removes guarded worktrees and keeps
the contract, events, outputs, verifier evidence, and manifest.

## Token policy

New ordinary-work contracts should set `limits.token_policy` to `estimate`.
Known numeric overruns and missing token telemetry are recorded as warnings;
they do not discard a candidate that passed the remaining gates. Unknown
telemetry remains unknown and is never converted to zero or invoiced. Set
`hard` explicitly for a canary that must stop on token overrun or unknown
telemetry. If the optional key is absent from an older immutable contract,
the controller applies legacy `hard` behavior. Wall/process timeouts,
attempts, no-progress, repeated-failure breakers, locks, isolation, and
independent acceptance remain enforced in either mode.
