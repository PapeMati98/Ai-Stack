# JSON schema — rag-engineering evaluator

The evaluator ships with a deterministic, stdlib-only schema validator (see
[`../scripts/evaluate_retrieval.py`](../scripts/evaluate_retrieval.py)). This
document is the canonical, human-readable reference for the same contract.

All three documents share the top-level `schema_version: "1.0.0"`. The
evaluator rejects any input with a different value with exit code 2.

## Gold corpus

```json
{
  "schema_version": "1.0.0",
  "corpus_id": "<string>",
  "license": "<public-domain|CC-BY-4.0|CC0-1.0|MIT|Apache-2.0|BSD-2-Clause|BSD-3-Clause>",
  "manifest": [
    {
      "source_id": "<lowercase-with-dashes, 3..64 chars>",
      "project_id": "<lowercase-with-dashes, 2..64 chars>",
      "title": "<string>",
      "revision": "<token, 1..64 chars>",
      "valid_from": "<ISO-8601 zoned | unknown>",
      "valid_until": "<ISO-8601 zoned | unknown | null>",
      "supersedes": ["<source_id>", ...],
      "superseded_by": "<source_id | null>",
      "aliases": ["<string>", ...]
    }
  ],
  "alias_groups": [
    {
      "alias_group_id": "<lowercase-with-dashes>",
      "canonical_source_id": "<source_id declared in manifest>",
      "other_source_ids": ["<source_id>", ...],
      "rationale": "<string explaining why the entities share a name>"
    }
  ],
  "forbidden_sources": [
    {
      "source_id": "<source_id declared in manifest>",
      "reason": "<string>",
      "never_cite": true
    }
  ],
  "cases": [
    {
      "case_id": "<lowercase-with-dashes>",
      "question": "<string>",
      "temporal_intent": "<current|historical|any>",
      "adversarial_kind": "<see below>",
      "expected": {
        "answer": "<string | null>",
        "answer_match_policy": "<exact|case_insensitive|contains|abstain>",
        "abstain_expected": "<bool>",
        "required_sources": ["<source_id>", ...],
        "allowed_revisions": ["<revision>", ...] | null,
        "forbidden_sources": ["<source_id>", ...],
        "must_disambiguate_alias_group": "<alias_group_id | null>"
      },
      "notes": "<string>"
    }
  ]
}
```

`adversarial_kind` must be one of:

- `current_vs_historical`
- `superseded_source`
- `alias_collision`
- `unknown_answer`
- `conflicting_evidence`
- `forbidden_project`
- `duplicate_advisory`
- `citation_revision`
- `abstention_positive`
- `baseline_correctness`

The `expected.answer_match_policy` controls how `expected.answer` is compared
to the predicted `answer`:

- `exact` — case-sensitive character-for-character equality.
- `case_insensitive` — case-folded equality.
- `abstain` — only `abstained=true` is correct; `expected.answer` MUST be null
  and `expected.abstain_expected` MUST be true. When `expected.abstain_expected` is
  false, `expected.answer` MUST be a non-empty string and `answer_match_policy`
  must not be `abstain`.

When `temporal_intent` is `current` or `historical`, `expected.allowed_revisions`
should list the revisions a correct prediction may cite. A citation whose
corpus-declared revision is not in this list triggers a
`temporal_revision_correctness=FAIL` verdict and a critical `invalid_citation`
flag.

`expected.must_disambiguate_alias_group` makes the case a false-merge trap.
The prediction must NOT claim all members of the alias group are a single
entity (the evaluator scans `rationale` and `answer` for unnegated merge
assertions such as `same entity`, `one and the same`, `merged into one`,
or `single entity`; explicit disambiguations such as `not the same entity`
or `distinct entity` are recognized and pass).

`forbidden_sources` declared inside `cases[].expected` extend the corpus-level
`forbidden_sources`. Citations referencing either fail
`project_isolation=FAIL` and add `forbidden_project_disclosure` to the
critical-reason list.

## Predictions

```json
{
  "schema_version": "1.0.0",
  "model_id": "<token, 1..64 chars>",
  "model_kind": "<baseline|candidate>",
  "predictions": [
    {
      "case_id": "<must equal a corpus.cases[].case_id>",
      "answer": "<string | null>",
      "abstained": "<bool>",
      "abstain_reason": "<string | null>",
      "citations": [
        {
          "source_id": "<must exist in corpus.manifest>",
          "chunk_id": "<string | null>",
          "page": "<int | null>",
          "revision": "<must equal corpus.manifest[].revision for that source>",
          "quote": "<string>",
          "confidence": "<float in [0,1] | null>",
          "timestamp_zone": "<UTC|unknown>",
          "retrieved_at": "<ISO-8601 zoned | unknown | null>"
        }
      ],
      "rationale": "<string>"
    }
  ]
}
```

When `abstained=true` the `answer` MUST be `null` or the empty string. The
evaluator counts any other shape as a critical answer-correctness regression.

`timestamp_zone` MUST be `UTC` or `unknown`. The evaluator refuses to accept
arbitrary IANA names — any future extension must remain opt-in and explicit.
The same constraint applies to `retrieved_at`: if present, it MUST be
ISO-8601 zoned or the literal string `unknown`. No freshness is inferred
from a missing timestamp; the report records `freshness_inferred: false`.

## Report (per-model)

```json
{
  "schema_version": "1.0.0",
  "report_kind": "single-model",
  "evaluator_version": "0.1.0",
  "corpus_id": "<string>",
  "model_id": "<string>",
  "model_kind": "<baseline|candidate>",
  "generated_at": "<ISO-8601 UTC>",
  "totals": {
    "cases": <int>,
    "predicted_cases": <int>,
    "answer_correctness_rate": <float 0..1>,
    "answer_correctness_scored": <int>,
    "abstention_correctness_rate": <float 0..1>,
    "abstention_correctness_scored": <int>,
    "citation_precision": <float 0..1>,
    "citation_recall": <float 0..1>,
    "temporal_revision_correctness_rate": <float 0..1>,
    "temporal_revision_correctness_scored": <int>,
    "project_isolation_pass_rate": <float 0..1>,
    "project_isolation_scored": <int>,
    "alias_resolution_pass_rate": <float 0..1>,
    "alias_resolution_scored": <int>,
    "no_false_merge_pass_rate": <float 0..1>,
    "no_false_merge_scored": <int>,
    "citation_format_valid_rate": <float 0..1>,
    "citation_format_valid_scored": <int>,
    "critical_failures": <int>,
    "critical_failure_cases": ["<case_id>", ...]
  },
  "overall_status": "<PASS|FAIL>",
  "failure_gates": ["<gate>", ...],
  "latency_observed": "UNKNOWN",
  "token_observed": "UNKNOWN",
  "cost_observed": "UNKNOWN",
  "freshness_inferred": false,
  "by_case": [<per-case verdict>, ...]
}
```

`overall_status` is FAIL whenever any failure gate fires. Averages are reported
but never used to mask critical failures.

## Comparison report (baseline vs candidate)

```json
{
  "schema_version": "1.0.0",
  "report_kind": "baseline-vs-candidate",
  "evaluator_version": "0.1.0",
  "generated_at": "<ISO-8601 UTC>",
  "baseline": { "model_id": "<string>", "overall_status": "<PASS|FAIL>",
                "failure_gates": [...], "totals": {...},
                "report_path": "<path>" },
  "candidate": { ... same shape ... },
  "delta": {
    "answer_correctness_rate": <float>,
    "abstention_correctness_rate": <float>,
    "citation_precision": <float>,
    "citation_recall": <float>,
    "temporal_revision_correctness_rate": <float>,
    "project_isolation_pass_rate": <float>,
    "alias_resolution_pass_rate": <float>,
    "no_false_merge_pass_rate": <float>,
    "citation_format_valid_rate": <float>,
    "critical_failures_baseline": <int>,
    "critical_failures_candidate": <int>,
    "candidate_fixed_critical": ["<case_id>", ...],
    "candidate_regressed_critical": ["<case_id>", ...]
  },
  "decision": "<promote|do not promote|insufficient evidence>",
  "improvement_claim": "NOT_MADE — averages alone do not justify a claim of improvement; the decision reflects critical-gate evidence only.",
  "notes": "<string>"
}
```

## Critical failure catalogue

The evaluator treats the following as critical:

- `forbidden_project_disclosure` — citation references a forbidden source.
- `false_merge` — case demands alias disambiguation but the prediction merges
  entities.
- `unsupported_critical_answer` — non-abstain question without a correct
  answer, or abstention where one was expected.
- `invalid_citation` — citation references an undeclared source_id or
  carries a revision that does not match the corpus declaration.
- `missing_prediction` — no prediction supplied for a known case.

Any of these flips `overall_status` to `FAIL` regardless of the average
metrics. The evaluator never applies weights to averages to mask a critical
regression.

## Privacy and safety

The evaluator never uploads documents, never calls a provider, never
modifies the Hub and never executes the system under test. All inputs must
already be sanitised; the evaluator will refuse to load anything that
violates the schema.