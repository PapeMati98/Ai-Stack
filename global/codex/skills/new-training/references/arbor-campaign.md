# Arbor CPU campaign and experience transfer

`arbor_campaign.py` provides a bounded, stdlib-only local CPU comparison for
the existing training and architecture workflows. It produces numeric
measurements; it does not produce an Agent Graph review, acceptance, or runtime.
Use this reference only for an explicitly requested Arbor comparison. Ordinary
training and coding sessions gain no new required steps.

## Staged blind campaign

Use separate train and selection datasets in JSON form:

```json
{"X": [[0.1, 0.2], [0.3, 0.4]], "y": [0.5, 0.7]}
```

The `prepare` command reads only those two inputs, trains the built-in
declarative candidates, selects on the selection split, and freezes the winner.
It writes campaign-ID-prefixed `campaign-locked`, `candidate`, local numeric
`unit-results`, `selection`, and `evidence` JSON files plus a frozen manifest.
No final dataset is generated or read by this stage. Different campaign IDs
can share an output directory; reusing an ID fails before overwriting files.

```powershell
python global/codex/skills/new-training/scripts/arbor_campaign.py prepare `
  --campaign-id arbor-pilot-1 `
  --output-dir .\runs\arbor-pilot-1 `
  --train-data .\public\pilot-1-train.json `
  --selection-data .\public\pilot-1-selection.json `
  --project arbor_pilot
```

After freeze, the parent creates a fresh final dataset from its separate
holdback outside the campaign directory and maker workspace. It must use the
same `X`/`y` JSON shape and must not have been used for candidate selection.
Then compute and consume its hash once:

```powershell
python global/codex/skills/new-training/scripts/arbor_campaign.py hash-dataset `
  --dataset ..\parent-controlled\arbor-pilot-1-final.json
python global/codex/skills/new-training/scripts/arbor_campaign.py evaluate-final `
  --campaign-dir .\runs\arbor-pilot-1 `
  --campaign-id arbor-pilot-1 `
  --dataset ..\parent-controlled\arbor-pilot-1-final.json
```

Compare the printed `hash-dataset` value with the parent-held dataset record.
The helper includes that hash in its pre-score claim and checks the persisted
manifest against campaign ID, the full candidate
configuration set, train and selection records, selected config and model-state
hashes, evaluator identity, and the supplied final dataset hash. It creates an
exclusive durable claim bound to the frozen manifest and final dataset before
model restoration or scoring. Restarted or concurrent attempts with that
campaign state are rejected. The claim is never cleared: a failure after claim
creation leaves the attempt spent and pending. This is durable process-level
fencing, not protection from a malicious OS owner or someone copying the entire
state directory. The helper cannot prove that the parent controlled the final
file; that custody is part of the parent UAT.

For a local synthetic CPU pilot, `prepare-synthetic` follows the same first
stage but generates only train and selection data. The parent still supplies an
independent final dataset after freeze:

```powershell
python global/codex/skills/new-training/scripts/arbor_campaign.py prepare-synthetic `
  --campaign-id arbor-campaign-1 --output-dir .\runs\arbor-campaign-1 `
  --seed 42 --noise 0.1 --collinear --project arbor_pilot
python global/codex/skills/new-training/scripts/arbor_campaign.py evaluate-final `
  --campaign-dir .\runs\arbor-campaign-1 `
  --campaign-id arbor-campaign-1 `
  --dataset ..\parent-controlled\arbor-campaign-1-final.json
```

`run-synthetic` is a same-command synthetic **demo**. It writes an
`arbor-<campaign-id>-DEMO_ONLY.json`
marker with `external_lockbox: false`; its final result is not blind-pilot
evidence. It fails if any output for the same campaign ID already exists.

## Verified lessons and campaign 2

Experience remains an append-only JSONL ledger with
`schema_version: arbor-experience/v1`. Canonical trust is `verified`,
`provisional`, or `rejected`; legacy `trusted` and `untrusted` inputs map to
`verified` and `rejected`. Trust defaults to `provisional`. A verified lesson
requires a non-empty evidence reference, an ISO `YYYY-MM-DD` evidence date,
and explicit transfer predicates. The helper records the current Python,
platform, and Arbor versions in both the environment and compatibility
predicates. Transfer requires a matching domain, project, environment/version,
and all predicates. A caller's `verified` declaration is not independently
reviewed by this helper; preserve the source evidence and obtain the normal
independent review before relying on a lesson outside its recorded conditions.

Infrastructure failures, including quota and provider interruptions, are
categorized separately and never returned as approach failures. Provisional,
rejected, stale-version, wrong-environment, missing-evidence, and context-mismatch
lessons are not applied at the default `verified` threshold.

After campaign 1's measurements and final result are available, record the
context-limited lesson with real evidence references and today's date:

```powershell
$evidenceDate = Get-Date -Format 'yyyy-MM-dd'
python global/codex/skills/new-training/scripts/arbor_campaign.py log-lesson `
  --experience-file .\runs\experience.jsonl `
  --campaign-id arbor-campaign-1 --unit-id unit-ridge-reg `
  --domain tabular_regression --project arbor_pilot `
  --trust verified --evidence-ref .\runs\arbor-campaign-1\arbor-arbor-campaign-1-evidence.json `
  --evidence-date $evidenceDate `
  --transfer-condition-json '{"collinear":true,"noise_lte":0.2}' `
  --hypothesis "State the measured, bounded hypothesis." `
  --summary "Summarize the observed selection and final result." `
  --disallow-algo linear_regression
```

The second campaign queries and applies only context-valid verified lessons;
the resulting candidate pool and actual selection are recorded in its evidence:

```powershell
python global/codex/skills/new-training/scripts/arbor_campaign.py prepare-synthetic `
  --campaign-id arbor-campaign-2 --output-dir .\runs\arbor-campaign-2 `
  --seed 999 --noise 0.1 --collinear --project arbor_pilot `
  --experience-file .\runs\experience.jsonl
python global/codex/skills/new-training/scripts/arbor_campaign.py evaluate-final `
  --campaign-dir .\runs\arbor-campaign-2 `
  --campaign-id arbor-campaign-2 `
  --dataset ..\parent-controlled\arbor-campaign-2-fresh-final.json
```

The parent creates a different fresh final dataset after campaign 2 freezes.
Do not reuse campaign 1's final data or inspect either final result to tune a
later candidate pool. PowerShell evaluates `$evidenceDate` on the evidence date.

## Local CPU results and explicit Agent Graph export

Local runs use `arbor-cpu-campaign/v1` and
`arbor-cpu-unit-result/v1`. They do not claim the AG `arbor-campaign/v1` or
`arbor-unit-result/v1` contract, independent checks, `ACCEPT`, or a worker
provider. Numeric results contain no synthetic reviewer verdict.

`export-ag` validates and copies a caller-authored native AG envelope. The
caller must supply actual native maker and independent reviewer candidate
lists, executable check command strings, relative `scope` and `inputs`, allowed
native pools/write globs, and a non-empty external `lockbox_ref`. It rejects
the old `relative_scope`/dictionary-check/string-maker shape, a local CPU pool,
absolute or parent-relative artifact paths, missing lockbox, and overlapping
maker/reviewer candidates. It checks structure only: it does not resolve
candidate IDs in the live profile inventory or execute checks. Export remains
pending AG's normal independent review and acceptance.

```powershell
python global/codex/skills/new-training/scripts/arbor_campaign.py export-ag `
  --spec .\parent-authored\native-ag-campaign.json `
  --output .\exports\native-ag-campaign.json
```

The output path must be new. No local CPU measurements or results are converted
into AG review results.

## Scope

This reference adds no ordinary-session trigger, mandatory read, model call,
daemon, separate review contract, or automatic training/deployment action.
Existing training safeguards and promotion gates remain in force.
