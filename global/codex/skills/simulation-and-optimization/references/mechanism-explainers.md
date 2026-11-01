# Mechanism explainer patterns (optional, from pinned "Anatomy" research)

Use these bounded prose patterns only when an illustration of a model or
policy mechanism is requested: state transitions, feedback loops, constraint
activation, Pareto-frontier movement or policy traces over scenarios. Normal
sessions add no reference reads, model calls, or work steps; the link in
`SKILL.md` adds a small static prompt-token cost.

These patterns add no simulator, optimizer, deployment target or control
channel. Simulation, optimization and execution remain separate trust
boundaries; this skill does not connect a policy to live control.

## Bounded mechanisms

- **Single state model, one timeline**: derive the visual trace and reported
  numeric series from the same seeded state at the same simulation time. A
  plotted trace and objective table must describe the same state.
- **Geometry invariants**: hold axes, units and scale fixed across scenarios
  and candidates so the quantities being compared remain comparable.
- **Contact-sheet sampling**: inspect fixed checkpoints (initial state,
  constraint activation, scenario boundaries and end) from the existing
  deterministic run. Reviewing those samples does not require another
  experiment.
- **Accessible alternatives depend on the medium**: for interactive HTML/SVG
  charts or previews, declare `prefers-reduced-motion`, keep controls keyboard
  reachable, and expose the same state in text or a table. For rendered MP4,
  provide captions, subtitles or an ordered text summary; an MP4 itself has no
  keyboard focus or ARIA tree, so do not claim those interactive properties
  for the file.
- **Visible simplifications**: label omitted details such as noise, uncertainty
  bands, rejected ladder rungs or shadow-mode gating. Do not present the
  illustration as a complete model or as V&V evidence.

## Deliberately out of scope

- No new solver, model, audit, execution path or mandatory reading step for
  ordinary sessions.
- No browser launcher, cleanup or publishing instructions.
- No 3D requirement; 2D charts and tables are preferred.
