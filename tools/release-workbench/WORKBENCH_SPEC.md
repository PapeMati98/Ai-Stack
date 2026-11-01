# Private publication workbench — shared implementation interface

Owner approved implementing a private, complete reusable AI Stack candidate after three named planning contributions. Implement the workbench and use ORIGINAL frozen source mechanisms; do not replace Fleet/Graph/Office/Hub/RSI with mock implementations. Public push, license/publisher, IAM, secrets, production and human contact remain separate owner gates. There is no public remote. Python standard library only.

## Product and file ownership

`release_workbench/exporter.py`, `privacy.py`, `configuration.py`, `provenance.py` are independent library nodes. Final integration owns `__main__.py`, `integration.py`, CLI tests and synthetic demo. Documentation owns README, docs/* and its check script. Do not edit another node's files or `__init__.py`. Parent-owned source inputs and output live in ignored private-inputs/out, never in tracked files. Tests use their own task-owned temporary directories, not host user directories.

## Shared interfaces (use these exact names)

All failures below raise ValueError with a useful non-sensitive error. CLIs catch errors, exit nonzero and never print matched secret values. Functions return JSON-serializable dictionaries with explicit version/status and findings/counts. APIs accept Path or str where a path is specified. No network, code evaluation, global config edits or git push.

### Exporter

`validate_manifest(manifest: dict) -> dict` validates version1, sources list and literal path/file entries. `export_candidate(manifest: dict, destination, *, allowed_output_parent) -> dict` exports pinned committed Git blobs and verifies expected SHA256. `verify_export(destination, receipt: dict) -> dict` verifies the exact output inventory and hashes, refusing extras/missing/changed files.

Manifest shape:
```
{"version":1,"max_total_bytes":209715200,"sources":[
 {"id":"source-label","repo":"/explicit/local/private/snapshot/repo","commit":"40-or-64-lowercase-hex",
  "files":[{"path":"relative/source.py","target":"runtime/package/source.py","sha256":"64hex"}]}]}
```
Reject unknown fields, invalid/duplicate IDs, empty lists, non-literal commits, absolute/backslash/drive/UNC/traversal/empty/dot/NUL paths, case-insensitive target collisions, reserved Windows filenames and ADS, Git metadata targets, symlink/gitlink/executable binaries, over-budget blobs and dangerous data paths/extensions (.env*, auth*.json, credentials*, secrets, vault, raw, proposals, logs/cache/state/databases, .git, node_modules, home profiles). Preserve legitimate auth.py source and LICENSE/NOTICE. Validate EVERY input/path/blob/hash before materializing outputs; do not use shell=True or source working-tree bytes. A failed manifest must not create files outside its explicit output parent, alter source repositories or overwrite an existing destination. One selected Fleet source snapshot is permitted, never the full archived release tree. Snapshot source commits are parent-created frozen copies with separately retained original provenance; do not pretend original uncommitted files were committed upstream.

Output receipt includes file source-label/commit/source-relative-path/target/sha256/bytes, total bytes and a deterministic tree hash over sorted target+bytes. Absolute source repo paths stay in the private caller evidence, not candidate files or public receipts. Receipt timestamps/absolute output locations must not affect reproducibility. No cleanup/delete of pre-existing artifacts.

### Privacy/security precheck

`scan_tree(root, *, policy=None) -> dict` recursively scans regular source files, refuses links/reparse points or unscannable/binary material by default, and returns status PASS/FAIL, files_scanned, findings (rule/path/line only) and limitations. `validate_policy(policy: dict) -> dict` accepts an optional version1 policy with literal `forbidden_literals` and an explicit reviewed allowlist of exact rule/path/content-hash findings. No values, line excerpts, reversible encodings or hashes of matched secret substrings in reports. Rule patterns detect recognizable credential/private-key material, credential assignments with actual values (not variables/placeholders), owner-machine paths and policy-specific topology/client strings. Distinguish harmless example.invalid, example.com, placeholders, loopback endpoints and license-author attributions from owner data. Do not blanket-delete/replace all e-mails, private-looking IPs or legal notices. An allowlist is not inferred from the filename 'test'. Tests must seed a credential in an innocently named file, unknown binaries and a symlink/junction escape; known benign fixtures remain clearly labeled.

A PASS means only the implemented LOCAL PRECHECK passed; never claim a formal complete secret audit, security certification, legal clearance or permission to publish. A full publication readiness report needs independent review and any configured real scanner evidence, otherwise that gate remains UNKNOWN.

### Configuration

`validate_config(data: dict, *, owned_root) -> dict` validates version1 config with exact keys: stack_root, state_root, runtime_root (relative under owned_root), enabled_clis (list of {agent,model}), external_integrations (empty by default), network_integrations_enabled (false by default), credential_references (mapping agent->opaque OS credential reference, never plaintext).

`render_template(text: str, variables: dict) -> str` substitutes ONLY explicit `{{NAME}}` placeholders with supplied strings; no eval, implicit environment expansion, traversal, HOME fallback, shell or interpolation of unknown keys. Missing/unused/unknown placeholders fail. `verify_portability(root, *, forbidden_literals=None) -> dict` returns pattern findings without matching values, covering original owner paths and supplied topology literals. Example config is secret-free. Native providers may use their existing authorized account through approved native auth; claiming 'zero reads outside workspace' would be false (runtime/system/credential access is separate). UAT must prove absence of accesses to PRIVATE BUSINESS STATE and production endpoints, not invent an OS sandbox from HOME alone.

### Provenance / rights inventory

`audit_provenance(manifest: dict, components: list[dict]) -> dict`. Component exact keys: id, targets (literal paths), source, license, notice_paths (literal paths), evidence, status (authored/third-party/unknown). Each exported file is covered exactly once by a component; unknown/unlicensed/not-proven evidence stays OPEN and blocks public readiness. Dedupe actual components/notice hashes, not repeated archival filename counts. Preserve existing notices and source attribution; new Git history grants no rights. `write_notices(report: dict, destination) -> dict` creates a NEW bounded UTF-8 Markdown inventory, without manufacturing license text or granting a top-level license. Parent/owner supplies rights assertions; empty evidence is never auto-approved.

### Final integration and maintainer CLI

Build `python -m release_workbench` subcommands with --help: export --manifest FILE --destination DIR --output-parent DIR --receipt FILE; verify --candidate DIR --receipt FILE; scan --candidate DIR --report FILE; config-check --config FILE --owned-root DIR; rights-check --manifest FILE --components FILE --report FILE; bundle-check --candidate DIR --receipt FILE --report FILE. JSON status and nonzero failure; output files are bounded and may not overwrite pre-existing reports without an explicit safe revision path. No publish/push operation is implemented.

`bundle-check` combines exact export verification, local scan, portability and a truthful gate report. Required gates are export_integrity, local_privacy_precheck, independent_security_review, real_secret_scanner, provenance_rights, clean_profile_real_cli_uat, license_owner_decision, publisher_owner_decision, public_push_owner_approval. Missing evidence yields UNKNOWN/PENDING, not PASS. The private candidate may be assembled/tested with those owner decisions pending; public_ready remains false. Neither a file token nor a magic phrase is authority for approval.

Prepare an actual synthetic fixture/demo and a parent-executable native-UAT driver; it must invoke the EXPORTED real Graph/Fleet and local isolated exported Office/Hub/RSI mechanisms with configured existing CLI accounts, no mocks labeled native, no production Office/Hub, no host knowledge-config fallback, real run IDs/model lineages/checks/repair evidence and no money/mail/browser/client effects. If a component or native harness cannot safely run, report that exact stage BLOCKED and preserve partial evidence; never return accepted based on a transcript fixture or a different private implementation. Explicitly separate offline fixture test, native execution, product acceptance and public-release readiness. Do not recursively plan or spawn ad hoc agents; a deterministic parent-owned UAT harness can execute the predeclared product user journey after code review.

## Source freeze and boundaries

Parent will supply `private-inputs/export-manifest.json` with actual pinned snapshot repo and per-file hashes, and a sanitized components draft. Their absolute paths are private inputs and are never emitted into candidate docs. Snapshot code is a source edition, not redistributed installed CLI binaries or the owner filesystem. Exclude all runtime state, credentials, customer evidence, vault contents and old source Git histories. Preserve attribution. Actual source may contain owner-specific code/doc defaults: report rather than silently making bulk substitutions that break syntax or security policies.

## Acceptance

Each code node needs meaningful regression/negative tests and an independent other-lineage reviewer. Tests are offline synthetic; parent alone runs actual source export and final native product UAT under isolated resources. Documentation must list current real limitations and label pending measurements/support, not promise one-command/cross-OS/security/ROI results. No new skill trigger, daemon, recurring audit, registry or ordinary-session step is added. No public publication or owner-license decision inferred from this work.
