# Synthetic native delivery UAT

Synthetic-only delivery-evidence summary. No external services, account data, or costs. Standard library only.

## Usage

```sh
python delivery_summary.py INPUT_FILE
python delivery_summary.py --help
```

`INPUT_FILE` is a UTF-8 JSON file whose top level is an ARRAY of run records
(consumed by `summarize_runs` in `delivery_evidence.py`). On success the CLI
prints one deterministic JSON object (sorted keys) to stdout and exits 0.
On invalid JSON, non-array shape, unreadable input, or an invalid record it
exits 2 with a short generic stderr message; no traceback and no echo of
record details.

## Semantics

- `unknown`: a field that is missing or null. Unknown is counted in
  `unknown_count`; only known values contribute to `known_sum`. Missing is
  distinct from explicit zero (zero is known, contributes 0).
- `FAILED` / `PARTIAL` / `CANCELLED`: preserved as ordinary statuses in
  `status_counts`; their known token and duration values are included in the
  sums exactly like `COMPLETED` runs. `CANCELLED` is never rewritten as
  success, and a missing status defaults to `UNKNOWN`.
- Quality `unknown` is independent of status: `COMPLETED` does not imply
  `ACCEPT`, and `FAILED` does not imply `REPAIR`.
- Cached/reasoning tokens: `cache_read_tokens`, `cache_write_tokens`, and
  `reasoning_tokens` are reported as their own sums and are NEVER added to
  `total_tokens` again. A missing `total_tokens` is never derived from
  `input_tokens`/`output_tokens`; it stays unknown (`known_sum: null`).
- Native duration sum vs entire delivery time: `duration_seconds.known_sum`
  is only the sum of known per-run `duration_seconds` values. It is not wall
  clock, queueing, retry, or end-to-end delivery time; runs with missing/null
  durations contribute to `unknown_count` instead of the sum.

## CLI cases

1. Help:
   `python delivery_summary.py --help` → exit 0, prints usage.
2. Valid summary:
   `python delivery_summary.py runs.json` → exit 0, deterministic JSON
   summary with sorted keys.
3. Invalid JSON:
   `python delivery_summary.py bad.json` → exit 2, generic stderr, no
   traceback.
4. Non-array top level (e.g. `{}`):
   `python delivery_summary.py object.json` → exit 2, generic stderr.
5. Invalid record (e.g. negative tokens, bad enum, missing `run_id`):
   `python delivery_summary.py invalid.json` → exit 2, generic stderr, no
   record details echoed.
6. Missing/unreadable file:
   `python delivery_summary.py does-not-exist.json` → exit 2, generic
   stderr.
