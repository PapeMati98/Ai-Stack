# ARCHITECTURE — source edition preparation

Part of AI Stack, licensed under the Apache License 2.0 (see the repository `LICENSE`).
Native integration evidence is pending. This workbench prepares and checks
source; the exported original stack remains responsible for its real native
Graph/Fleet/Office/Hub/RSI behavior. No hosted service is provided.

## Public APIs and boundaries

The [shared specification](../WORKBENCH_SPEC.md) defines the contract. Library
failures use `ValueError`; JSON reports distinguish local results from rights,
native execution, independent acceptance and owner decisions.

| Module | Public calls |
| ------ | ------------ |
| [exporter](../release_workbench/exporter.py) | `validate_manifest(manifest)`, `export_candidate(manifest, destination, *, allowed_output_parent)`, `verify_export(destination, receipt)` |
| [privacy](../release_workbench/privacy.py) | `validate_policy(policy)`, `scan_tree(root, *, policy=None)` |
| [configuration](../release_workbench/configuration.py) | `validate_config(data, *, owned_root)`, `render_template(text, variables)`, `verify_portability(root, *, forbidden_literals=None)` |
| [provenance](../release_workbench/provenance.py) | `audit_provenance(manifest, components)`, `write_notices(report, destination)` |
| [integration](../release_workbench/integration.py) | `offline_demo(workspace, source)`, `bundle_check(candidate, receipt)`, `native_uat(candidate, receipt, config, workspace, plan, *, execute=False)` |
| [CLI](../release_workbench/__main__.py) | Exact command reference in [QUICKSTART](QUICKSTART.md) |

## Exact data contracts

The export manifest has `version: 1`, optional `max_total_bytes`, and a
nonempty `sources` list. Each source has exactly `id`, `repo`, `commit`,
`files`; each file has `path`, `target`, `sha256`. `repo` is an explicit local
snapshot path kept in private inputs; `commit` is a literal lowercase 40- or
64-digit hash. File/target paths are literal forward-slash relative paths.
Unknown fields, traversal and case-insensitive target collisions are rejected.
The exporter reads committed Git blobs, checks declared SHA256 and budgets
before materializing output, and refuses existing destinations. Source checkout
bytes and old Git history are not copied into the candidate.

Receipt file rows use `source`, `commit`, `source_path`, `target`, `sha256`,
`bytes`; the receipt also contains `version`, `status`, `total_bytes` and
`tree_hash`. Absolute source repository locations and timestamps are absent
from reproducibility identity. Exact inventory verification rejects extra,
missing or altered candidate files. Keep receipts and generated reports
outside that inventory.

Privacy policy keys are `version: 1`, optional `forbidden_literals`, and
optional `allowlist`. Each reviewed allowlist entry has exactly `rule`, `path`,
`content_sha256` (the entire file's hash). `validate_policy` returns validation
counts, not a policy to pass onward: retain the original policy dictionary.

Configuration requires exactly `version`, `stack_root`, `state_root`,
`runtime_root`, `enabled_clis`, `external_integrations`,
`network_integrations_enabled`, `credential_references`. Roots are literal
relative paths beneath an explicit existing `owned_root`. CLI entries contain
`agent` and `model`; integrations default to an empty list and network
integrations remain false. Credential references map enabled agents to opaque
`os-credential://provider/handle` references; validation never resolves them.
Template syntax is uppercase `{{NAME}}`; missing, unused, malformed or unknown
placeholders fail. Portability takes a source-tree root, not a config object.

Provenance components have exactly `id`, `targets`, `source`, `license`,
`notice_paths`, `evidence`, `status`; status is `authored`, `third-party` or
`unknown`. Every target needs exactly one component. `inventory_status` may be
`PASS` for exact structural coverage while rights `status` remains `OPEN`:
supplied evidence is not independently authenticated. `write_notices` creates
one new bounded Markdown inventory, preserves the need for existing notices,
and supplies no license text or top-level license grant.

## Offline lifecycle versus native acceptance

```text
explicit pinned manifest -> export -> receipt -> exact verify
  -> local privacy and portability checks -> bundle local_status
explicit config -> config-check
manifest + components -> rights-check -> OPEN inventory / notices

parent-reviewed real source candidate + isolated Git workspace + plan
  -> native-uat preparation -> parent execution -> independent acceptance
```

`offline_demo` uses real synthetic Git commits and two real blob exports; it
does not execute models. `bundle_check` combines integrity, local privacy and
portability. Locally clean bundles report `status=PENDING`,
`local_status=PASS`, `public_ready=false`; independent and owner gates remain
unresolved. It does not ingest external review/approval evidence or promote a
separate rights report into a public-ready decision.

The native driver requires distinct receipt-bound exported Python entrypoints
for Office, Hub, RSI, Graph and Fleet, in that dependency order, and a distinct
exported OS isolation runner. The plan binds enabled maker/reviewer agents,
models and different lineages, stage arguments, expected outcomes, evidence
paths and bounded timeouts. Graph runs as maker; Fleet runs as reviewer.
Inspect the actual plan validation in [integration.py](../release_workbench/integration.py)
before constructing the parent's plan; preparation is no substitute for the
original tools' own CLI contracts.

The product journey must implement a synthetic TODO, obtain genuine independent
review, repair and recheck findings, exchange scoped Office status/handoff,
record the Hub session, and capture RSI failure/repair evidence where applicable.
Stage adapters must invoke those original mechanisms. The driver's shape/hash
checks cannot authenticate a runner's assertions or guarantee this journey
happened. Independent reviewers must inspect real run IDs, provider/model
lineages, changed artifacts, checks, repair results and enforcement evidence.

Without `--execute`, a valid plan reports `PREPARED`. Missing dependencies or
failed stages report `BLOCKED`, preserving completed stage records. Successful
process/evidence capture reports `EXECUTED`, with independent product acceptance
still `UNKNOWN`. There is no automatic timeout retry or private host fallback.
Retain incomplete and failed runs; a new attempt needs new evidence paths.

Redirected HOME and an environment allowlist are not an OS sandbox. Native
providers may use separately authorized OS account credentials; private business
state and production endpoints must be denied by real isolation enforcement.
No host Office/Hub/RSI state or knowledge-config fallback is permitted. The
preparation tools preserve useful autonomous DEV/TEST behavior; native execution
uses only the explicitly authorized synthetic journey and approved accounts.
