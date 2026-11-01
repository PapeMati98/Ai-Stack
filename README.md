# AI Stack — coordinated work across native coding assistants

Source edition of a private, single-operator agent stack, published under the
[Apache License 2.0](LICENSE). It is an engineering release, not a supported
product: read [Validation status](docs/VALIDATION_STATUS.md) for what was
actually tested.

AI Stack combines reusable skills, native CLI execution, a dependency scheduler,
durable inter-agent coordination, shared project knowledge and a repair helpdesk.
Its main unit of work is a short TODO: an executor produces an artifact, real
checks run, and a reviewer from another model lineage accepts it or requests a
targeted repair. A Graph is useful when tasks have real parallel branches or
dependencies; a small task uses one maker/reviewer pair.

This candidate collects the original implementation while its independent
installation is being validated. The release boundary excludes private Git history, operator accounts, transcripts,
mail, wiki contents, customer records, runtime state, model weights and CLI
binaries. A source edition does not transfer anyone's subscription or access.

## Components

| Component | Source | Purpose |
|---|---|---|
| Native Fleet | `fleet-suite/` | Provider adapters, model and mode validation, quota evidence, pinned deployment and recovery |
| Graph and common skills | `global/codex/skills/` | TODO execution, independent review, historical context routing and reusable task guidance |
| Agent Office | `shared/agent-office/` | Presence, explicit durable messages, handoffs and coordination |
| Knowledge Hub | `shared/knowledge-hub/` | Source-grounded context, session capture and reviewed knowledge workflows |
| RSI Helpdesk | `global/codex/skills/rsi-helpdesk/` | Durable incident filing and bounded repair execution |
| Optional bridges | `shared/` and `global/tools/` | Desktop, browser, connector and service integrations with separate prerequisites |
| Release workbench | `tools/release-workbench/` | Pinned source export, exact inventory verification, privacy checks and provenance reports |

Optional connectors are source integrations, not services enabled by cloning
the repository. They require their own configured environment and permissions.
The release evidence must state which combinations actually passed; inclusion
of an adapter is not proof that all providers or operating systems were tested.

## Start safely

1. Read [Getting started](docs/GETTING_STARTED.md) and
   [Security boundaries](docs/SECURITY_BOUNDARIES.md).
2. Run the [offline synthetic walkthrough](tools/release-workbench/docs/QUICKSTART.md).
   It needs Python and Git, uses no model account and makes no external calls.
3. Inspect a fresh-profile installer plan before installing anything. Keep
   explicit profile and state directories separate from an existing stack.
4. Configure only the native providers you actually have permission to use.
   Preserve their data-use, billing and model identity checks. Run the
   synthetic native journey and retain its real execution and review evidence.

The offline walkthrough and native journey are different evidence. Neither
one constitutes security certification, legal clearance or permission to publish.
The [24-case offline reference](examples/delivery-evidence/README.md) and
[validation status](docs/VALIDATION_STATUS.md) describe their separate scope.

## What makes a delivery credible

An accepted delivery has the actual artifact, executable checks, the native
execution identity and an independent product review. A successful message,
scheduled task or process exit alone is insufficient. Failures and unavailable
measurements remain visible. Cached input and reasoning are not added again to
a provider's total-token figure.

Model selection follows configured capability classes and current evidence.
Unknown quota is unknown; a local token ledger is not an account-balance API.
There is no paid fallback implied by a failed subscription or free trial.

## Status and licensing

This is an engineering release, not a supported general release. See
[Validation status](docs/VALIDATION_STATUS.md) for completed checks and
remaining gaps. The repository is licensed under the [Apache License 2.0](LICENSE)
(see also [NOTICE](NOTICE)). Existing third-party notices remain with their
components and keep their own licenses, for example the vendored TypeScript
under `fleet-suite/payload/agent-fleet/scripts/vendor/typescript/`.

Client and project names in tests and examples are fictional placeholders.

Do not copy private production policies or qualification receipts into a public
installation. Configure your own trusted policy and prove your own provider
readiness. No model account, credential, production endpoint or access grant is
included in this repository.
