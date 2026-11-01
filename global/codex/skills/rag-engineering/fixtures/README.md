# Synthetic gold corpus for the rag-engineering evaluator

This directory contains a small, **deterministic, public-domain** gold corpus and
accompanying prediction bundles used by the
[`evaluate_retrieval.py`](../scripts/evaluate_retrieval.py) evaluator and its
test suite.

## Files

| File                              | Purpose                                            |
| --------------------------------- | -------------------------------------------------- |
| `gold-corpus.json`                | Gold corpus with 10 cases covering all adversarial kinds required by the task. |
| `baseline-predictions.json`       | Stub baseline prediction bundle, intentionally exposes several critical failures. |
| `candidate-predictions.json`      | Stub candidate prediction bundle, with a smaller set of failures. |
| `malformed/`                      | Adversarial inputs for corpus and prediction validation tests. |

## License

All content in this directory is **public-domain**. The corpus is synthetic:
no customer data, credentials, restricted documents, mail, calendar, payment
or CRM information is included. Project names (`atlas`, `hyperion`, `falcon`)
and entity names (`Phoenix`, etc.) are placeholders and refer to no actual
system.

## Why each adversarial case exists

- `current-vs-historical-question` — must privilege the current handbook revision.
- `superseded-source-trap` — must NOT cite only the superseded revision.
- `historical-question-allowed-revision` — must use the historical archive for
  a historical-intent question.
- `unknown-answer-must-abstain` — must abstain and refuse to disclose the secret.
- `abstention-positive` — must abstain on an unannounced product.
- `alias-collision-phoenix` — must NOT merge two distinct entities that share a name.
- `conflicting-evidence-release-notes` — must privilege the current revision over
  the draft.
- `duplicate-advisory-required` — must disclose that the advisory is duplicated
  across two channels.
- `forbidden-project-falcon` — must NOT cite the Falcon source under any
  circumstance.
- `baseline-correctness` — straightforward lookup that both bundles should pass.

## Critical gates

The evaluator refuses to PASS any report that has:

- A citation referencing a forbidden project source.
- A false merge between alias-group members.
- An unsupported answer to a non-abstain question.
- A citation referencing a source_id not declared in the corpus.
- A citation whose revision does not match the corpus-declared revision.
- An unzoned or non-`unknown` timestamp.