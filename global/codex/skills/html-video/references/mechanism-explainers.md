# Mechanism explainer patterns (optional, from pinned "Anatomy" research)

Use these bounded prose patterns only when a mechanism illustration is requested.
Normal sessions add no reference reads, model calls, or work steps; the link in
`SKILL.md` adds a small static prompt-token cost. The existing html-video
pipeline remains the renderer and its isolation rules remain authoritative.

## Bounded mechanisms

- **Single state model, one timeline**: derive the visual layer and numeric
  labels from the same state object at the same timestamp. Separate sources
  can make the diagram and its values disagree.
- **Geometry invariants**: keep node positions and aspect stable for a fixed
  viewport, anchor labels to their nodes, and keep edge endpoints on node
  borders. Only the intended state should move.
- **Contact-sheet sampling**: review fixed seek points (start, transition
  checkpoints and end) in the contact sheet already produced by the render
  pipeline. This inspects the existing output; it does not require another run.
- **Accessible alternatives depend on the medium**: for interactive HTML/SVG
  previews, declare `prefers-reduced-motion`, keep controls keyboard reachable,
  and expose the same state in text. For a rendered MP4, provide captions,
  subtitles or an ordered text summary; an MP4 itself has no keyboard focus or
  ARIA tree, so do not claim those interactive properties for the file.
- **Visible simplifications**: label omitted details such as retries, error
  paths or scaling in the frame or accompanying captions. Do not imply that a
  simplified mechanism is complete.

## Deliberately out of scope

- No new runner, harness, model, audit or mandatory reading step for ordinary
  sessions.
- No browser launcher, cleanup or publishing instructions; the existing
  `security-and-isolation.md` remains authoritative.
- No 3D or WebGL requirement; 2D SVG/DOM is the default.
