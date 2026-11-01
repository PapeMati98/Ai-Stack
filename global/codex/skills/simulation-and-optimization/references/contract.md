# Simulation and optimization contract

Return a model card and result record:

- `use_case`: decision, horizon, actors, state, actions and success metric.
- `model`: equations or rules, units, time base, parameters, assumptions and provenance.
- `data_and_uncertainty`: source/hash, calibration, distributions, missingness and sensitivity.
- `vv`: verification tests, validation cases, acceptance thresholds and known limitations.
- `baseline_and_ladder`: simplest baseline, selected energy ladder rung and rejected rungs.
- `scenarios`: nominal, OOS, stress and tail cases with reproducible seeds.
- `results`: objective values, feasibility, violations, robustness, cost and latency.
- `pareto`: objectives, dominance rule, frontier solutions and chosen tradeoff.
- `control_gate`: shadow-mode evidence, safety envelope, fallback, human approval and rollback.
- `decision`: research-only, shadow-only, revise or blocked.

Energy ladder: (1) rule or heuristic, (2) MILP/MPC, (3) forecast plus optimizer,
(4) evolutionary parameter or policy search, (5) RL only after simulator V&V,
(6) shadow mode with safety envelope and deterministic fallback. For storage,
always report economics, degradation/cycles, constraint violations, tail scenarios,
stability, OOS generalization and deterministic-baseline comparison; reward alone
is insufficient. Never connect a policy to live control from this skill.
