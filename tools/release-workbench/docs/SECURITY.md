# SECURITY — local precheck, not clearance

Part of AI Stack, licensed under the Apache License 2.0 (see the repository `LICENSE`).
Native integration evidence is pending. A local `PASS`, matching hashes, test
counts or new Git history do not establish complete secret/security clearance,
rights ownership, safe native execution or permission to publish.

## Local scanning contract

`scan_tree(root, *, policy=None)` inspects a caller-selected source directory.
It refuses links/reparse points, sensitive paths, unknown/binary encodings,
oversized files and unreadable material as failure findings. Recognizable
private keys, credential values, token patterns and owner-machine paths are
checked alongside explicit policy `forbidden_literals`. These finite rules
cannot discover every secret or all private topology.

Findings contain only `rule`, relative `path`, and `line`; reports also include
status, counts and limitations. They do not include matched values, snippets,
reversible encodings or hashes of secret substrings. Benign placeholders,
example domains and loopback endpoints are recognized in supported contexts;
there is no blanket exemption for test files or arbitrary addresses. Existing
legal notices and attribution must be preserved.

A version-1 policy may carry exact reviewed `allowlist` entries with `rule`,
`path` and full-file `content_sha256`. Any content change invalidates that
exception. The validator returns summary counts, so the scanner must receive
the original policy dictionary. The CLI scanner uses its default policy;
custom policies use the library API. See [QUICKSTART](QUICKSTART.md).

## Explicit unresolved release gates

`bundle-check` reports these nine required gates, plus a local portability gate:

| Gate | Evidence represented here |
| ---- | ------------------------- |
| `export_integrity` | Exact candidate inventory and committed-blob receipt verification |
| `local_privacy_precheck` | Implemented local rules only |
| `independent_security_review` | `UNKNOWN`; independent review required |
| `real_secret_scanner` | `UNKNOWN`; actual scanner evidence required |
| `provenance_rights` | `UNKNOWN`; separate inventory and independent rights review required |
| `clean_profile_real_cli_uat` | `UNKNOWN`; genuine isolated native journey required |
| `license_owner_decision` | `PENDING`; explicit owner decision |
| `publisher_owner_decision` | `PENDING`; explicit owner decision |
| `public_push_owner_approval` | `PENDING`; explicit owner approval |

The current bundle command never sets `public_ready=true`. Local cleanliness
returns `PENDING` with exit 2; a local failure returns `FAIL` with exit 1. It
does not load an approval token, review file, or prior native report as authority.
The rights inventory independently stays `OPEN` even when coverage is `PASS`.

## Native isolation and safe evidence

The offline synthetic quickstart performs no native provider calls. Parent-only
`native-uat --execute` launches exported mechanisms via an exported OS isolation
runner and may use existing authorized native accounts. Python/runtime/system
files and approved OS credential access are distinct from private business state:
there is no claim of zero reads outside a workspace. Redirected HOME, environment
variables and hashes alone do not establish confinement.

Before native acceptance, verify real enforcement artifacts denying private
business state and production endpoints. Require isolated exported Office, Hub
and RSI resources, synthetic TODO/review/repair data, genuine independent CLI
lineages and actual run IDs. No host knowledge/configuration fallback, production
connection, browser/mail action, new cost or publication is authorized. Missing
isolation, provider availability or evidence must remain a stage-specific
`BLOCKED` result. `PREPARED` means no native execution; `EXECUTED` records process
evidence and still requires independent acceptance. Runner assertions are not
self-authenticating proof.

Never commit secrets, customer data, tokens, keys, cookies, `.env` files, auth
stores, raw native logs or private source paths. Use opaque
`os-credential://provider/handle` references in configuration; validators never
resolve them. Keep private source manifests and original provenance outside
tracked documentation. Office carries statuses, counts and opaque references,
not secret values or source excerpts. Preserve failed evidence and choose new
report paths; do not delete pre-existing artifacts. Route suspected exposure
privately to the assigning maintainer without pasting the sensitive value.
