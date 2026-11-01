# SUPPORT — private maintainer handoff

Private candidate. **No license grant or public publication approval.**
Native integration evidence is pending. This repository supplies local source
preparation tools; there is no SLA, hosted service, public issue endpoint or
response-time commitment. The original RSI mechanism is an integration target,
not a hosted support service supplied by this workbench.

## Reproduce before escalating

Use Python 3.11+ and local Git for the [offline quickstart](QUICKSTART.md).
From the checkout root run:

```console
python -B -m release_workbench --help
python -B -m unittest tests.test_docs
```

There is no measured support matrix or verified cross-OS/native-provider
compatibility table. A passing run proves only that run's synthetic offline
checks. Native account availability, real source hardening, package sealing,
clean-profile native UAT and independent product acceptance remain parent
integration work. No one-click installation or measured ROI is claimed.

| Symptom | Interpretation and next step |
| ------- | ---------------------------- |
| `rights-check` exits 2 / `OPEN` | Inventory is not legal clearance; retain findings for independent rights review |
| `bundle-check` exits 2 / `PENDING` | Local gates may pass; inspect `local_status` and unresolved external/owner gates |
| Output collision / `FAIL` | Keep existing bytes; choose a new receipt/report revision path outside the candidate |
| `native-uat` / `PREPARED` | Plan validated; nothing native was executed |
| `native-uat` / `BLOCKED` | Preserve the named stage and earlier evidence; report the specific missing dependency to the parent |
| `native-uat` / `EXECUTED` | Process evidence captured; independent product acceptance is still required |

## Private problem report

Use the private channel that assigned the work; this document authorizes no
external human contact. Include a synthetic reproducer, command/API and flags
with local paths redacted, exit code, relevant status/gate/stage, Python/Git
versions and an opaque reference to retained evidence. Review reports before
sharing: even metadata can contain private paths. Do not attach entire native
logs, customer data, credential stores, source manifests or production topology.

Retain prior failed/partial receipts and runs. Do not repeatedly retry an
unidentified provider or stalled transport: after two unproductive native
transport timeouts, escalate to the parent with the stage, attempts and missing
provider identity/output. The driver itself does not automatically retry.
Do not substitute another private implementation or a mock success.

The parent arranges independent other-lineage review, actual source export,
isolated native integration and product acceptance. License, publisher, rights,
public push, production, IAM and any new cost stay with their explicit owner
decisions. Useful autonomous DEV/TEST features remain available within the
approved scope. See [SECURITY](SECURITY.md) for evidence boundaries.
