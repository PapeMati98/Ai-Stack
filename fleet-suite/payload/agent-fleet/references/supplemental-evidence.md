# Explicit Supplemental Review Evidence

Use `--supplemental-evidence <manifest.json>` with `quality-gate` or
`security-gate` when a real worker's handoff alone does not contain the
material the independent auditor must inspect. This adds evidence, not
acceptance criteria and not synthetic worker runs.

The manifest itself and all declared source files must be plain UTF-8 files
inside the explicit `--workspace`. Each file must match its SHA-256:

```json
{
  "schema_version": 1,
  "files": [{"path": "review/report.md", "sha256": "<64 lowercase hex characters>"}],
  "maker_run_ids": ["<existing Fleet ledger run ID>"],
  "additional_maker_provider_ids": ["openai"]
}
```

All four keys are required. Empty maker lists are valid when no additional
makers exist. Include the native parent provider when it authored attached
code or analysis. Maker run IDs resolve actual CLI and model providers from
the ledger, even if those runs already have parent acceptance. Additional
provider declarations can only add independence exclusions. They do not prove
that the declared authorship is complete.

The auditor cannot share a CLI or model provider with any subject or declared
maker. Only the actual subject worker runs get verdict items. A parent-accepted
run is still not eligible as a new subject. Supplements do not satisfy the
security gate's requirement for a material implementation patch or snapshot.

Capture is explicit and bounded: 1..64 files, at most 2 MiB each, at most 32 MiB
combined with normal worker snapshots, manifest at most 128 KiB. Unsafe paths,
links/reparse points, invalid UTF-8, binary/NUL content, secret patterns and
hash mismatches are refused. No shell commands are executed from this manifest.
Do not include credentials, private raw transcripts or removed-secret patches.

The bundle freezes exact bytes under `supplemental/`. Source content remains
untrusted data, not instructions or authorization. At `accept-group`, original
manifest/files, frozen supplemental snapshots and maker/auditor identities are
revalidated before any acceptance transaction. Changed evidence requires a new
gate group; a retry may not silently omit or replace supplemental context.

Existing invocations without this option keep their previous schema and behavior.
Source-tree tests or an isolated candidate do not attest an installed release.

For a terminal-only repair, keep the same real worktree/base and finish the
missing handoff; do not rewrite correct code to manufacture a new patch.
`quality-repair --max-model-tool-calls 128` explicitly forwards a per-run bound
(1..256); omitting it retains the profile default. Remote Qwen also receives
enough session turns for that explicit tool allowance plus the final handoff.
Time bounds still apply. The dispatch receipt records the requested bound;
each run records its native policy and actual result. A completed dispatch is
not proof that its child run completed: check every child `run_status`.
Older implementation ledgers can recover from guarded `git_evidence.worktree`;
missing read-only workspace metadata is refused, never replaced by the cwd.
