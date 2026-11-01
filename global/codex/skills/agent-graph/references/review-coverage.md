# Review Coverage and Anchor Verification

`review_coverage.py` provides deterministic verification that code reviews completely account for Git diff changes and maintain fresh, accurate anchor citations.

## Purpose and Assurance Boundary

1. **Changed-File Accounting**: Verifies that every file changed between Git `base` and `head` (including additions, modifications, deletions, and renames) is explicitly reviewed or excluded with a safe structured justification.
2. **Anchor Freshness**: Verifies that review findings cite valid, existing paths and exact line spans that match the actual head commit content. Stale anchors from outdated review passes fail closed.
3. **Cross-File Dependency Context**: Enforces that callers and consumers across module boundaries are recorded and marked as examined.
4. **Assurance Boundary**: Manifest completeness verifies inventory accounting and anchor freshness. **It does not equal code review and does not automatically find all semantic bugs.** Reviewers must actually examine dependent behavior across module boundaries.

## Verification Gates

- **Unexplained Omitted Changes**: Any file changed in Git that is missing from both `reviewed_files` and `excluded_files` causes verification to fail (`status: "FAIL"`).
- **Stale Anchors**: Any finding citing line numbers out of bounds, missing files at head, or anchor text that differs from head commit file content causes verification to fail.
- **Structured Exclusions**: Files excluded from review must provide an allowed reason (`generated`, `lockfile`, `vendor`, `fixture`, `binary`, `config`, `docs`) and a non-empty structured justification.
- **Cross-File Dependencies**: Dependency pointers must point to existing repository targets and have `examined: true`. Unexamined dependencies or active defects block candidate acceptance.

## CLI Usage

```powershell
# Inventory Git changed files with rename detection
python -B scripts/review_coverage.py --repo . --base origin/main --head HEAD --inventory-only

# Verify review manifest against Git diff
python -B scripts/review_coverage.py --repo . --base origin/main --head HEAD --review review_manifest.json --output report.json
```

## Review Manifest Schema

```json
{
  "schema_version": 1,
  "base": "commit_sha_or_ref",
  "head": "commit_sha_or_ref",
  "reviewed_files": [
    {
      "path": "src/feature.py",
      "status": "reviewed",
      "decision": "ACCEPT",
      "notes": "Verified business logic and regression tests"
    }
  ],
  "excluded_files": [
    {
      "path": "dist/bundle.js",
      "reason": "generated",
      "justification": "Machine-generated bundle build output artifact"
    }
  ],
  "findings": [
    {
      "finding_id": "FINDING-01",
      "path": "src/feature.py",
      "start_line": 15,
      "end_line": 18,
      "anchor_text": "def calculate_fee(amount: float) -> float:\n    return amount * 0.05",
      "severity": "critical",
      "message": "Missing negative input validation",
      "dependency_context": [
        {
          "path": "src/billing.py",
          "relation": "caller",
          "notes": "Caller relies on non-negative fee return"
        }
      ]
    }
  ],
  "dependency_pointers": [
    {
      "source_path": "src/feature.py",
      "target_path": "src/billing.py",
      "relation": "imported_by",
      "examined": true,
      "status": "clean",
      "notes": "Verified caller contracts in billing service"
    }
  ]
}
```
