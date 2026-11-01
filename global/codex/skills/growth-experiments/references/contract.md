# Growth experiment contract

Return an experiment card plus result record:

- `hypothesis`: mechanism, falsifiable prediction and rationale.
- `design`: population, unit, control, variants, allocation, exposure and dates.
- `metrics`: primary, secondary, guardrails, attribution window and minimum effect.
- `instrumentation`: event names, properties, identity rules, QA evidence and gaps.
- `analysis`: estimator, exclusions, missingness, multiplicity and stopping rule.
- `result`: counts, effect with uncertainty, segment checks and guardrail status.
- `decision`: ship, iterate, stop or inconclusive, with owner and rollback trigger.

Do not call an underpowered, contaminated or post-hoc analysis conclusive. Preserve
the pre-registered plan and negative results. No automatic campaign, budget,
experiment, tracking or production change is allowed.
