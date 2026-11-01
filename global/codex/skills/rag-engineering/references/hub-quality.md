# Hub quality adapter (conditional reference)

Read this file only when the task explicitly assigns Hub pilot preparation or
repair. This reference does not create a general retrieval-evaluation trigger.
For general retrieval evaluation, use `scripts/evaluate_retrieval.py`
and `references/json-schema.md` instead.

## What it is

`scripts/hub_quality.py` is a thin preparation / strict validation
adapter. It does NOT grade answers itself: all answer and citation
metrics come from the existing evaluator (`evaluate_retrieval.py`).
The adapter adds only:

1. **Frozen inputs** — `freeze` hashes materials + questions
   (sha256) into a manifest; `project` / `evaluate` / `compare`
   refuse to run when inputs changed after the freeze
   (fail-closed `freeze mismatch`).
2. **Input normalization and provenance** — accepts the frozen public
   shape (`materials[]` with `source_id` and `facts[].fact_id`, plus
   `questions[]` with `q_id`, `type` and `text`) and hashes the original
   bytes. The external gold must use the explicit `hub-quality-gold-v1`
   object format; prose gold is rejected. A fixture `license` must be explicitly
   declared in that external gold; the adapter never assigns or changes upstream
   material rights. Raw prediction files are not rewritten; their byte hash and supplied run/model/timing/usage metadata
   are recorded, with missing values left `UNKNOWN` and explicit zero kept.
3. **Verbatim quote layer** — each citation is checked against the actual
   facts of its source, including citations attached to abstentions. A
   correctly quoted historical or duplicate fact is reported without a
   false gate when the question context asks for that evidence; using stale
   evidence for a current-value question still fails. Conflict context and
   cross-project comparison are advisory. Explicit forbidden-source and
   false-merge rules remain evaluator gates. Matching proves quote presence,
   not semantic support, entailment, freshness or authority.

All derived outputs refuse existing paths, input aliases and output paths
whose components include a symlink or junction. The adapter does not
silently overwrite a gold or raw-prediction artifact.

## CLI (offline, no Hub client)

```console
python scripts/hub_quality.py freeze    --materials M --questions Q --out freeze.json
python scripts/hub_quality.py project   --materials M --questions Q --gold G --freeze-manifest freeze.json --out corpus.json
python scripts/hub_quality.py evaluate  --materials M --questions Q --gold G --predictions P --freeze-manifest freeze.json --out report.json
python scripts/hub_quality.py compare   --materials M --questions Q --gold G --baseline B --candidate C --freeze-manifest freeze.json --out cmp.json
```

## Tests

`scripts/test_hub_quality.py` — deterministic unit/CLI regressions for
freezing and projecting the supplied public inputs, schema and ID validation,
output-path protection, provenance, abstention citations, and contextual stale,
duplicate, conflict and cross-project behavior. Synthetic gold and
predictions are test-only; they are not live caretaker evidence.
The bundled `fixtures/hub-quality-pilot/` contains public synthetic inputs only,
with no independent answer key. These are smoke/development fixtures, not a
fresh final benchmark after a model or prompt has been tuned on them.

## Boundaries

No second metrics engine, no answer generator, no live Hub client,
no production/deploy side effects. The independent private Hub key
and any post-freeze ML final dataset never enter this workspace.
