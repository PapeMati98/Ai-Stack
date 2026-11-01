# Baseline comparison (opt-in)

Use only when a TODO actually compares, migrates, or rebuilds behavior against a
known baseline. Do not load this for ordinary acceptance, small local fixes, or
as a new acceptance contract. The active path remains `../SKILL.md` and the
existing TODO requirement rows.

## What to record

Reuse the existing TODO rows. Do not invent a parallel form:

| Existing row | Baseline addition |
| --- | --- |
| requirement | Which owner-required behavior is being compared or rebuilt |
| expected | Expected behavior now, plus the baseline behavior it replaces |
| actual | Observed behavior on the current build |
| environment | Unchanged rules from `environment-safety.md` |
| evidence | Existing evidence kinds, plus baseline artifact reference |

Add these only when the change actually needs them:

- `baseline`: stable reference to the prior accepted artifact, capture, or
  record (id, date, path). One baseline per compared requirement is enough.
- `state`: persisted state before and after the compare or migrate step, when
  state is part of the behavior.
- `viewport`: role, surface/route, and viewport or work-area when the compared
  behavior is user-visible. Fill from `user-perception-and-feedback.md`; do not
  copy that file here.

## Owner-required features

- Owner-required features stay required. A skip or N/A cannot remove one without
  an authorized scope change recorded in the TODO. Silent omission is not
  acceptance.
- Missing mandatory behavior remains not accepted regardless of any aggregate
  score, average, or percentage.
- Partial migration leaves the unmapped owner-required behavior as a visible
  gap in the same TODO, not as a new follow-up process.

## Pixel diff

A pixel or visual diff is supporting evidence only. It is not UAT and cannot
replace role, route, expected/actual, `surface-reachability`, `action-feedback`,
or `visible-failure-recovery`. A zero pixel delta does not prove the user path
works.

## Boundaries

- Do not edit historical `scripts/acceptance.py`.
- Do not introduce a score calculator, weighted rubric, or new acceptance
  contract. Report function | exists | tested | integrated | missing as usual.
- Do not repeat all-system tests for every task. Retest the compared or rebuilt
  behavior and material regressions only.
- Do not add a second reviewer, audit-of-audit, or extra Hub read for the
  comparison itself. Independent product review already required by `../SKILL.md`
  covers it.
- Report-format repair does not justify recoding or another full test cycle.
