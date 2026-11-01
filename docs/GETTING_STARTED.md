# Getting started with a source candidate

These instructions describe the actual source layout. A source candidate is
private until its licensing, confidentiality review and publication approval
are complete. Use explicit task-owned directories for evaluation.

## Prerequisites

- Python 3.11 or newer and Git for the release workbench and scheduler.
- Windows for the Windows-native adapters and launchers covered by this edition.
  Other operating systems require their own tested native dependencies; their
  presence in source does not establish support.
- Separately installed official provider CLIs and authorized native accounts
  for paid-subscription-backed model execution. The offline example needs none.
- Optional service and browser integrations remain disabled until configured
  and independently tested with the required access.

No account credentials, model binaries or private user profile should be copied
into the source tree. Subscription availability and data-use controls are
operator responsibilities, separate from the source license.

## Offline first

The [release workbench quickstart](../tools/release-workbench/docs/QUICKSTART.md) exercises a synthetic Git repository, pinned
blob export, integrity verification, privacy checks, portable configuration,
provenance reporting and negative cases. Run the documented commands from the
`tools/release-workbench` directory containing the `release_workbench` package. Its `OPEN`, `PENDING` and `BLOCKED`
results retain their meanings; they are not installation successes.

## Inspect the real installers

The following commands display the supported arguments without installing:

```console
python -B fleet-suite/scripts/install_router_suite.py --help
python -B shared/agent-office/install_agent_office.py --help
python -B global/codex/skills/agent-graph/scripts/todo_graph.py --help
```

Fleet's source installer accepts explicit `--package-root`, `--home`,
`--appdata` and `--state-root` before its action. Its `plan` action is the
read-only preflight. The minimal shared deployment uses `--tools shared`;
`codex` is not a supported installer target name. A real `deploy` requires
the exact package fingerprint. Preserve displaced artifacts with `--no-delete`.
Run `verify` against the genuine receipt returned by that deployment.

Agent Office has its own installer and package seal. Its explicit `--home`,
`--appdata`, `--state-root` and `--python-executable` precede the action.
Use `dry-run` first. Do not reuse a production remote configuration for a
synthetic evaluation. Knowledge Hub likewise has its own initialization and
explicit vault/configuration binding; a global home override is insufficient
evidence that every business-state path is isolated.

After changing packaged source, use the original package's real sealing
operation before deployment. Do not edit fingerprints, receipts or completion
markers to simulate an accepted installation.

## Native evaluation

Use a separate synthetic workspace and a small TODO with nonempty checks.
One qualified CLI implements it and a different model lineage reviews it.
Test the repair path with a clearly labeled controlled fault, then verify the
fixed artifact. Retain actual native run IDs, model identity, tests, verdicts,
elapsed time and known usage. Preserve FAILED and unknown values.

Use explicit bindings for the evaluation's Office runtime, Hub configuration,
telemetry and incident queue. Run Office send/read/ACK and Hub session capture
against those isolated stores. An RSI queue entry proves filing; it does not
prove a complete autonomous repair cycle.

Native authentication may remain in its provider's approved native store. It
must not be exported or copied into the evaluation bundle. Application-level
scope controls and separate configuration are not an operating-system sandbox.
Document the actual enforcement and residual access of each tested adapter.

## Recovery and support

Keep the exact installer input, receipt and previous private version. For a
fresh-profile evaluation, deactivation and retaining artifacts is sufficient;
there is no need to alter an existing installation. Follow the actual receipt-
based recovery command only for a deployment you own and understand.

Report reproducible source defects with a redacted minimal example. Omit
credentials, customer context, real mailbox/browser state and raw model session
logs. Support scope and release claims must match the accompanying validation
evidence; untested configurations remain untested.
