# Architecture Flow Composition (JSON-configured)

Opt-in upgrade for the `html-video` skill: an animated, data-driven architecture
flow diagram (nodes, edges, moving packets, illustrative counters and a flow log)
built from a strict JSON config. It reuses the existing `render_video.py` engine,
the timeline contract, and the visual conventions of
`compositions/typography_chart.html` (dark palette, header/footer, safe margins,
card/panel treatment). It is **not** a new renderer, screen, or navigation
pattern, and it changes nothing about sandboxing, route isolation, asset
pinning, or determinism checks.

Use this only when the task actually calls for a flow/architecture diagram. The
telemetry chart workflow in `SKILL.md` remains the default.

## Build

```powershell
python -B (Join-Path $SkillRoot 'scripts\build_architecture_flow.py') `
  --config (Join-Path $SkillRoot 'compositions\architecture_flow.example.json') `
  --out (Join-Path $SkillRoot 'compositions\my_flow.html')
```

- Output is created atomically (`O_CREAT|O_EXCL`): an existing path is refused
  unless `--force` is given, and `--force` only ever rewrites a plain regular
  file. Symlinks, junctions, and other reparse points — as the config input,
  the output, or any of their parent directories — are always refused as
  unsafe aliases. Nothing is ever removed.
- Exit codes: `0` built, `2` config/schema error, `3` output error
  (exists / invalid suffix / unsafe alias).
- The shipped `compositions/architecture_flow.html` is generated from
  `compositions/architecture_flow.example.json` by exactly this command.

Then render with the unchanged engine, e.g.:

```powershell
python -B (Join-Path $SkillRoot 'scripts\render_video.py') render `
  --composition (Join-Path $SkillRoot 'compositions\architecture_flow.html') `
  --out-dir "output\flow_16_9" --aspect "16:9" --duration 6.0 --fps 30
```

16:9, 9:16 and 1:1 are supported through the standard `?aspect=&w=&h=` interface.

## Config schema (v1, strict)

Unknown keys anywhere are rejected. All strings are data only — they reach the
DOM exclusively via `textContent`/`createElementNS`; there are no
style/HTML/URL/path fields. Errors report the JSON path and a stable reason
only: the raw offending key, value, or filesystem path is never echoed, so a
hostile key such as `password=...` cannot leak into logs or stderr.

| key | type | bounds / rules |
| --- | --- | --- |
| `schema_version` | int | must be `1` |
| `title` | string | required, 1–120 chars, no control chars |
| `subtitle` | string | optional, ≤200 chars |
| `badge` | string | optional, ≤60 chars |
| `telemetry_mode` | enum | optional; only `"illustrative"` — measured/live modes are rejected |
| `duration_sec` | number | optional, finite, 1.0–30.0 (default 6.0) |
| `fps` | int | optional, 12–60 (default 30) |
| `packets_per_edge` | int | optional, 1–8 (default 3) |
| `grid.cols` / `grid.rows` | int | optional, 1–6 (default 4×3) |
| `nodes` | list | required, 2–24 items |
| `edges` | list | required, 1–48 items |
| `counters.records_target` / `error_budget` / `queue_max` | int | optional, 0–1,000,000 |
| `log_messages` | list | optional, ≤12 entries, each ≤120 chars |

Node: `{id, label, kind, col, row}` — all required. `id` must match
`^[a-z0-9][a-z0-9_-]{0,31}$` and be unique; `label` ≤64 chars; `kind` one of
`source|process|sink|store|gate`; `col`/`row` inside the grid and unique per
node (position collisions are rejected, which keeps the layout overlap-free).

Edge: `{id, from, to, kind?, label?}` — `from`/`to` must reference existing node
IDs; self-loops and duplicated ordered pairs are rejected; `kind` one of
`data|control|event`; `label` optional ≤48 chars.

See `compositions/architecture_flow.example.json` for a complete synthetic
example (Polish labels wrap per node and truncate with an ellipsis, so long
labels stay legible and contained in every orientation).

## Layout and typography

The diagram is the primary product, so the SVG is laid out directly in the
rendered pixel space of its card viewport (`viewBox` == container pixels,
scale 1) instead of a fixed abstract grid shrunk by `preserveAspectRatio`.
Node size, node-label, edge-label, and kind-tag font sizes are computed from
the actual cell size in real pixels, with floors (node-label ≥ 16 px,
edge-label ≥ 14 px, kind-tag ≥ 12 px before the final fit pass), and a
deterministic fit pass after `document.fonts.ready` shrinks any label that
still exceeds its box. In 9:16 and 1:1 the counters/log compress to a compact
strip so the diagram card takes the majority of the frame. The UAT records the
computed rendered label sizes per aspect in `geometry_audit.json` and fails if
they drop below the legibility floor (16/13/11 px rendered).

## Safety and determinism contract

- **Illustrative by construction**: v1 only supports `telemetry_mode:
  "illustrative"`. The composition shows a persistent `ILUSTRACYJNE · DANE
  POKAZOWE` badge and a "not production telemetry" tag, so example counters
  cannot impersonate real Office/production metrics.
- **Injection-safe**: the config is embedded as `<script type="application/json">`
  with `&`, `<`, `>` and U+2028/U+2029 escaped — a closing `</script>` sequence
  can never appear in the blob, and the blob still round-trips through
  `JSON.parse`. Palette and geometry come from fixed enums in the template.
- **Timeline contract**: exposes `window.renderTimeline` (+`window.seek`) and
  the `data-timeline-ready` marker. Packet positions, the highlighted edge, the
  visible log prefix, and all counters are computed from one pure function of
  `t`; forward, backward, and random seeks — and fresh browser instances —
  agree exactly. Frame zero already shows the full diagram. Because the engine
  samples frames at `t = i/fps` strictly below `duration`, any seek inside the
  final frame interval (`t >= duration - 1/fps`) snaps to the completed flow
  state, so the rendered MP4 provably ends on the full log prefix, 100%
  progress and complete counters. No `Date.now`, `performance.now`,
  `Math.random`, timers, or autonomous `requestAnimationFrame`.
- **Isolation**: no fetch, WebSocket, or external resources; the generated file
  is a single self-contained ~30 KB HTML with `assets: []`, rendered under the
  unchanged route isolation and asset pinning of `render_video.py`.

## Tests and UAT

Unit tests (schema, injection, duplicate IDs, invalid edges/times, output
refusal; retain fixtures under `--artifact-root`):

```powershell
python -B (Join-Path $SkillRoot 'scripts\test_architecture_flow.py') `
  --artifact-root "<owned-evidence-dir>"
```

UAT (real Chrome renders through the current `render_video.py` in unique
retained run directories: 16:9 and 9:16 MP4 covering the composition's full
declared timeline with ffprobe validation, manifest/composition-hash check,
zero-network/no-page-error isolation audit of BOTH render manifests, DOM
geometry/no-overflow audit at frame zero AND at the completed end state
(horizontal and vertical text containment, populated log-line clip check) with
recorded rendered label sizes against the legibility floor, deterministic
forward/backward/random seek and fresh-browser states, completion-state audit
of the last sampled frame, hostile-label safety, contact sheets, and a
scenario expected/actual `uat_report.json` with SHA-256 evidence hashes):

```powershell
python -B (Join-Path $SkillRoot 'scripts\architecture_flow_uat.py') `
  --artifact-root "<owned-evidence-dir>"
```
