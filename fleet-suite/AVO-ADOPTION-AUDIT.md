# AVO adoption audit

## Scope and provenance

- source: `https://github.com/gatordevin/avo`
- pinned revision: `f6dad9e639d3c9e5d5ac9ccacbe076d82cfa39d2`
- acquisition: shallow clone into a unique temporary quarantine
- execution policy: repository code, evaluators, tests, MCP server, and install
  commands were not executed
- static skill scan: `ALLOW`, 108 files, zero matched findings
- semantic security verdict: `BLOCK` for installation or direct reuse

`ALLOW` from the static scanner means only that its signatures found no known
pattern. It is not a trust decision. Manual semantic inspection found execution
boundaries that are incompatible with this stack.

## Blocking findings

1. `scoring.py` can execute evaluator and baseline subprocesses with the full
   inherited environment.
2. Target evaluators dynamically import candidate Python or build and load
   candidate C code.
3. Unattended Claude execution defaults to `bypassPermissions`.
4. The API presents a sandbox boundary but accepts arbitrary `bash -lc` work.
5. The MCP surface accepts caller-selected targets, run locations, and output
   paths while triggering execution and mutation.
6. Prompt/data boundaries are weak, and dependencies and CI actions are not
   consistently revision-pinned.

No malicious intent was established. The result is nevertheless **do not
install, do not register its MCP server, and do not copy executable code**.

## Patterns adopted as clean-room behavior

Agent Loop 0.3.0 reimplements only high-level workflow ideas behind existing
Fleet security boundaries:

- explicit baseline and target contract;
- correctness before optimization;
- measured baseline and frozen minimum improvement;
- quality score vector rather than a prose-only verdict;
- accepted and rejected trajectory with a dead-end ledger;
- one substantial frozen hypothesis per isolated attempt;
- stagnation detection and bounded independent supervisor redirect;
- tie budget and acceptance-rate reporting;
- parent-owned champion acceptance.

The implementation uses the existing Agent Fleet transport, detached worktree
controls, trusted verifier, Quality Gate, immutable evidence, token circuits,
kill switches, and exact-hash approval. It does not import AVO code.

## Patterns deliberately rejected

- arbitrary shell, evaluator, compiler, dynamic import, or MCP execution;
- inherited ambient credentials or provider secrets;
- bypass-permission flags;
- target-selected execution/output paths;
- automatic destructive Git operations, commit, merge, push, or deploy;
- cumulative mutation of a previous candidate;
- same-session or same-CLI self-review presented as independent supervision;
- automatic champion integration.

## Independent review evidence

- Codex native audit subagent: semantic verdict `BLOCK`; completed independently
  from the implementation design.
- Agent Fleet architecture review:
  `run-20260825T164825Z-c5c930de05`; Grok 4.6; `REJECTED` because the provider
  run exhausted its bounded execution without a valid handoff (591,768 tokens,
  201 seconds). Its content was not used as approval evidence.

## Candidate 0.8.21 audit disposition

The instruction adapter scan is `ALLOW` with zero findings. The canonical
Agent Loop payload scan reports five `W007-secret-read` matches. Manual line
review adjudicates all five as scanner false positives:

- four matches are validation messages or field access for the contract key
  `kill_switch.env`; none reads a `.env` file or secret store;
- one match creates `secret.txt` inside a unique test directory to prove that a
  junction escape is rejected; it neither reads nor exports a credential.

No new L2-search file reads `.env`, SSH, cloud credential, keychain, cookie, or
token storage. Provider execution remains inside Agent Fleet; the trusted
verifier receives a sanitized environment. Candidate semantic verdict:
`ALLOW_FOR_BUILD`, with deployment still prohibited by
`release_state=candidate`.

Deterministic verification is 341/341 PASS. The 19 installer tests, including
exact rollback, were executed only in a deleted temporary copy promoted to
`released` for the test fixture.

This document is an adoption record, not authorization to deploy candidate
release 0.8.21.
