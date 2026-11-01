# Offline delivery-evidence reference

This is the original synthetic reference: a standard-library summary library,
its CLI, requirements, and the accepted contract and review-regression tests.
It makes no model, account, provider, Office, Hub, or API calls. The offline
checks are separate from the earlier native journey described in the
[validation status](../../docs/VALIDATION_STATUS.md).

## Run the 24 offline checks

With Python 3.13, copy all six files from `reference/` into a fresh,
task-owned workspace and run these commands from that workspace:

```sh
python -B contract_tests.py library
python -B contract_tests.py cli
python -B review_regression_tests.py
```

These run 12 library, 6 CLI, and 6 review-regression cases, respectively.
The CLI and review checks launch local Python subprocesses and create
synthetic files in `.test-inputs` within that workspace. The tests' Python
audit guard and environment filtering are limited checks, not an OS sandbox.
See [the reference README](reference/README.md) for the CLI input and usage.

## Reading the summary

`total_tokens` is the provider-reported total, not a total recomputed from
components; cache and reasoning counters are reported separately and never
added again. A missing or null value increments `unknown_count`; zero is a
known value. `known_sum` is null when no values are known. Failed, partial,
and cancelled runs retain their statuses and known usage; quality is
independent of status. The sum of known durations is not end-to-end wall time.

## Optional fresh rebuild

To repeat the exercise with your own `agent-graph`, create a fresh task
workspace seeded only with `reference/requirements.md`,
`reference/contract_tests.py`, and `reference/review_regression_tests.py`.
Ask your graph to rebuild the implementation there using your own Hub context
and qualified native accounts. Do not seed it with the reference implementation.
That separate native exercise may use your configured providers; the offline
checks above do not.
