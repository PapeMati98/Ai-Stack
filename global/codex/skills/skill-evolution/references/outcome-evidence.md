# Outcome evidence for focused trials (opt-in)

This extends the existing blind create-packet/resolve flow. Ordinary sessions
and legacy score trials gain no required fields, reads, questions, steps,
model calls or councils. It grants no release/deployment authority.

Declare `outcome_trial: true` in the specification before `create-packet`.
Freeze `baseline_artifact_sha256` and `candidate_artifact_sha256` in each case
using hashes of the artifacts independently tested for that case.
A specification declaration is preserved in the packet and sealed mapping;
it cannot be downgraded by omitting observations from the evaluation.
Opting in only during evaluation leaves the expected artifacts unknown and
cannot support acceptance. Legacy score-only specifications stay unchanged.

| Signal | Allowed values | Meaning |
|---|---|---|
| discovery | true / false / unknown | Necessary skill content reached |
| action | true / false / unknown | Requested operation actually performed |
| correctness | true / false / unknown | Independent check of the current artifact |

For acceptance, both anonymous variants of every case need comparable,
known observations. Each observation explicitly binds `variant`,
`condition_sha256` and `artifact_sha256`. The observed artifact hash must match
the case's frozen expected hash, including the baseline. Missing bindings are
not filled in. Different cases may produce different artifacts; there is no
global single-artifact constraint. The result's `outcome.artifact_bindings`
maps each case to expected and observed baseline/candidate hashes.
Candidate signals must all be true; false action/correctness/necessary
discovery blocks acceptance even with a higher textual score. Missing or
unknown evidence yields NEEDS_EVIDENCE. Existing critical veto, independent
actor/provider lineage checks, blind labels and score comparison remain.

Each signal stores `value`, optional `detail` (up to 500 characters), and
optional `evidence_ref` (up to 200 characters). Use opaque check/run IDs and
hashes; never prompts, code, transcripts or credentials. These references
are never executed. A hash binds an observation to material; it does not
prove correctness. The independent evaluator must actually obtain current
test evidence. The controller validates the supplied bindings and signals,
not the truthfulness of a caller's attestation.

## Synthetic CLI example

From the skill directory, save the following as `spec.json`. The hashes are
illustrative: replace them with the fixed condition and tested artifact
hashes for a real trial. The evaluator receives only `packet.json`, never
`sealed.json`, and scores the anonymous answers independently.

```json
{
  "trial_id": "outcome-example",
  "target_skill": "synthetic-skill",
  "condition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "outcome_trial": true,
  "baseline_proposer": {
    "actor_id": "baseline",
    "provider_lineage": "synthetic-a"
  },
  "candidate_proposer": {
    "actor_id": "candidate",
    "provider_lineage": "synthetic-b"
  },
  "cases": [
    {
      "case_id": "case-1",
      "case_type": "comprehension",
      "prompt": "Perform a synthetic operation",
      "expected_behavior": "Independently checked correct artifact",
      "baseline_answer": "Older response",
      "candidate_answer": "Revised response",
      "baseline_artifact_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "candidate_artifact_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  ]
}
```

```powershell
python scripts/focused_trials.py create-packet --spec spec.json --packet-out packet.json --sealed-out sealed.json
python scripts/focused_trials.py resolve --packet packet.json --sealed sealed.json --evaluation evaluation.json --output result.json
```

Example `evaluation.json`:

```json
{
  "trial_id": "outcome-example",
  "evaluator": {
    "actor_id": "evaluator",
    "provider_lineage": "synthetic-c"
  },
  "case_evaluations": [
    {
      "case_id": "case-1",
      "variant_a": {
        "score": 6,
        "critical_failure": false,
        "outcome": {
          "variant": "variant_a",
          "condition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "artifact_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "discovery": {
            "value": true
          },
          "action": {
            "value": true
          },
          "correctness": {
            "value": true
          }
        }
      },
      "variant_b": {
        "score": 8,
        "critical_failure": false,
        "outcome": {
          "variant": "variant_b",
          "condition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "artifact_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
          "discovery": {
            "value": true
          },
          "action": {
            "value": true
          },
          "correctness": {
            "value": true
          }
        }
      }
    }
  ]
}
```

For the no-action negative path, set `action.value=false` on both anonymous
variants and resolve again: acceptance must be blocked. Restore action and
set `correctness.value=false` on both variants for a wrong-artifact path:
acceptance must also be blocked despite the score difference. Setting a
signal to `unknown`, omitting a binding, or omitting baseline observations
leaves the comparison incomplete. Replacing just one observed hash with a
different valid SHA-256 also blocks acceptance; a well-formed stale hash is
not current evidence. Anonymous expected hashes in `packet.json` follow each
case's deterministic A/B mapping; use those labels for real evaluations.
The resolution always carries
`release_authority: false`; SHADOW_ACCEPTED is not production activation.
