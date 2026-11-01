# Manifest schema — private candidate exporter

Private release-preparation contract only. Not a publication, licensing, or
rights clearance decision. Actual source export and native UAT are handled by
the parent integration; this document describes the import format for the
`exporter` node only.

## Shape

```jsonc
{
  "version": 1,
  "max_total_bytes": 209715200,
  "sources": [
    {
      "id": "fleet-snap",
      "repo": "/abs/path/to/frozen/source/repo",
      "commit": "<40-or-64 lowercase hex commit>",
      "files": [
        {"path": "git/tree/path.py", "target": "candidate/dir/path.py",
         "sha256": "<64 lowercase hex of committed blob bytes>"}
      ]
    }
  ]
}
```

## Rules

- The manifest is an object with exactly `version`, `max_total_bytes`,
  `sources`. Unknown or missing top-level fields are rejected.
- `version` must be `1`. `max_total_bytes` must be a positive int (cap 2^40).
- `sources` is a non-empty list. Entries have exactly `id`, `repo`, `commit`,
  `files`. `id` matches `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}` and is unique.
- `repo` is an explicit absolute local path to a frozen snapshot repository.
  It is used at import time only and never appears in candidate files or
  receipts.
- `commit` is a literal 40/64-char lowercase hex object id (no `HEAD`,
  branches, tags, or ranges) and must resolve to a commit object.
- `files` is a non-empty list of exactly `path`, `target`, `sha256`.
  `sha256` is the 64-char lowercase hex of the committed blob bytes.
- `path` names one committed file blob at `commit` via `git ls-tree` /
  `git cat-file`. Trees, gitlinks/submodules, symlinks, and executables are
  refused; source objects are read, never modified.
- `target` is the candidate-relative output path. Both `path` and `target`:
  POSIX separators only; no absolute, Windows drive, UNC, backslash, NUL,
  `.`/`..`/empty segments, trailing space/dot segments, ADS colons, or
  reserved stems (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).
- Dangerous data routes are denied on both ends: `.env*`, `auth*.json`,
  `credentials*`, `secrets*`, plus segments `secrets`, `vault`, `raw`,
  `proposals`, `logs`/`log`, `cache`, `state`, `databases`, `.git`,
  `node_modules`, credential/profile dirs (`.aws`, `.ssh`, `.gnupg`,
  `.azure`, `.kube`, `.docker`, `.config`), and home-profile basenames.
- Targets must be unique case-insensitively; source `(repo, commit, path)`
  pairs must be unique. Total blob bytes must fit `max_total_bytes`.
- Every entry is fully prevalidated (tree presence, object type, blob hash,
  budget) before any output file is written. Any failure leaves the
  destination untouched when no file was written yet, and never overwrites
  an existing destination or mutates the source repository.
- The receipt `{version, status, files, total_bytes, tree_hash}` records
  `{source, commit, source_path, target, sha256, bytes}` per file ordered
  by target, with a deterministic `tree_hash` over target/sha/bytes rows.
  Receipts contain no absolute source or destination paths.

## Attribution and scope

- Per-file `source` ids preserve attribution of the selected Fleet payload;
  no release archives are copied.
- `verify_export` refuses missing/extra/changed files, links and reparse
  points, receipt shape errors, and `tree_hash`/`total_bytes` drift.
- Local checks do not constitute security, rights, or publication
  readiness; publishing remains a separate owner approval.
