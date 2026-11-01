# AI Stack release workbench

This local workbench prepares a private, complete reusable source candidate
from explicitly selected, pinned Git snapshots. Python 3.11+ and the standard
library provide export verification, privacy/configuration prechecks, rights
inventory, a maintainer CLI, an offline demo, and a parent-operated native-UAT
driver. Local Git is required for the synthetic export demo.

Part of AI Stack, licensed under the Apache License 2.0 (see the repository `LICENSE`). The workbench's
own `public_ready` gate is a tool output for the candidate you prepare with it;
it does not describe this repository.

## Current evidence and remaining work

| Area | Current state |
| ---- | ------------- |
| Exporter, privacy, configuration and provenance libraries | Implemented; exact interfaces in [WORKBENCH_SPEC](WORKBENCH_SPEC.md) |
| Maintainer CLI | Implemented: `export`, `verify`, `scan`, `config-check`, `rights-check`, `bundle-check`, `demo`, `native-uat` |
| Offline synthetic lifecycle | Implemented; [QUICKSTART](docs/QUICKSTART.md) exercises the real CLI and committed synthetic Git blobs |
| Native-UAT driver | Implemented; preparation and execution are separate; missing dependencies produce a stage-specific `BLOCKED` report |
| Actual source export, hardening and package sealing | Parent integration work; the offline demo supplies no evidence for the real source bundle |
| Real exported Graph/Fleet and isolated Office/Hub/RSI | Native integration evidence is pending; no successful native product acceptance is established here |
| Independent product/security review and real secret scanner | Pending evidence; local tests do not substitute for these reviews |
| Rights, license, publisher and public push | Rights remain open; owner decisions remain pending |

The workbench implements the preparation workflow. Completing its unit tests
does not complete the original stack's native integration or public-release
acceptance. No measured cross-platform support, ROI, one-click installation,
or hosted service is claimed.

## Start locally

Run from this checkout with Python 3.11+ and local Git available:

```console
python -B -m release_workbench --help
python -B -m unittest tests.test_docs
```

The documentation test executes the [synthetic quickstart](docs/QUICKSTART.md),
including negative acceptance checks. It uses only fixtures created under its
own temporary directory in this checkout, with no provider/model calls,
package installation, or production integration. No `publish` or `push`
subcommand exists.

## Maintainer references

- [QUICKSTART](docs/QUICKSTART.md): executable offline journey and exact CLI flags.
- [ARCHITECTURE](docs/ARCHITECTURE.md): APIs, schemas, receipts and genuine native journey.
- [SECURITY](docs/SECURITY.md): local precheck limits, isolation and release gates.
- [SUPPORT](docs/SUPPORT.md): reproducible reports, current limitations and handoff.

Real snapshots, source provenance, runtime state, credentials, customer data,
and native execution evidence stay outside tracked candidate documentation.
Existing attribution and copyright notices must be preserved. These tools do
not replace the original Graph, Fleet, Office, Hub or RSI implementations or
disable their useful autonomous DEV/TEST features.
