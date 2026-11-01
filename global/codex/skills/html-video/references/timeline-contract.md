# Timeline Contract for HTML Video Compositions

This document defines the interface and execution contract between HTML/CSS/SVG video compositions and the `html-video` frame-accurate rendering engine.

## Core Principles

1. **Pure Time Function**: The visual state of the DOM at time `t` must be a pure, deterministic function of `t`.
2. **No Autonomous Clock**: The page must never rely on wall-clock time (`Date.now()`, `performance.now()`), autonomous timers (`setInterval`, `setTimeout`), or unconstrained animation loops (`requestAnimationFrame`).
3. **No Unseeded Randomness**: `Math.random()` is prohibited. If pseudo-random variation is required, use a deterministic seeded generator with a constant seed.
4. **Frame-Addressed Stepping**: The renderer controls time advancement by calling `window.seek(t)`. The composition updates the DOM immediately (or returns a Promise that resolves when the DOM is ready).
5. **Local Asset Pinning**: All assets (fonts, images, SVGs, audio) must reside locally within the composition bundle and be declared in the manifest. Remote resources over HTTP/HTTPS are blocked at the browser network layer.

---

## JavaScript API Contract

Every composition must expose either `window.renderTimeline` or `window.seek(t)` globally on `window`:

```javascript
window.renderTimeline = {
  // Duration in seconds (positive float)
  duration: 5.0,

  // Nominal frames per second (e.g. 30, 60)
  fps: 30,

  // Default viewport dimensions (pixels)
  width: 1920,
  height: 1080,

  // Declared aspect ratio ('16:9', '9:16', '1:1', etc.)
  aspectRatio: '16:9',

  // List of local relative asset file paths declared by this composition
  assets: [
    'assets/logo.svg',
    'assets/chart_data.json'
  ],

  // Core seek method: updates DOM to display exact state at time t
  // t is in seconds: 0.0 <= t <= duration
  // May be synchronous or return a Promise
  seek: function(t) {
    // 1. Clamp t: const clamped = Math.max(0, Math.min(t, this.duration));
    // 2. Compute normalized progress: const p = clamped / this.duration;
    // 3. Update DOM nodes, styles, SVG attributes, and text counters.
  }
};

// Global shorthand required by the renderer:
window.seek = function(t) {
  return window.renderTimeline.seek(t);
};
```

---

## Lifecycle and Readiness

Before rendering starts, the engine navigates to the composition URL and waits for the following readiness gates:

1. `document.readyState === 'complete'` (all static HTML and scripts evaluated).
2. `document.fonts.ready` promise resolved (all web fonts loaded and rendered).
3. Image completion: all `HTMLImageElement` nodes have `.complete === true` and `.naturalWidth > 0`.
4. `window.seek` function is callable.
5. DOM readiness marker: `document.documentElement.getAttribute('data-timeline-ready') === 'true'`.

---

## Determinism and Seek-Order Invariance

The composition must guarantee that:
$$\text{DOM State}(t) \equiv \text{DOM State}(t)$$
regardless of whether $t$ was reached by:
- Sequential forward stepping: $0.0 \to 0.1 \to 0.2 \to \dots \to t$
- Backward stepping: $5.0 \to 4.0 \to 3.0 \to t$
- Random seeking: $2.4 \to 0.5 \to t$

Any stateful transition logic (e.g. cumulative counters, physics accumulators, particle systems) must compute values analytically from $t$ rather than through step-by-step numerical integration with variable $\Delta t$.

---

## Viewport and Responsive Layout

Compositions should support URL parameters to adapt to target dimensions and aspect ratios:

- `?aspect=16:9&w=1920&h=1080` (landscape default)
- `?aspect=9:16&w=1080&h=1920` (portrait mobile)
- `?aspect=1:1&w=1080&h=1080` (square social)

Guidelines:
- **Margins**: Maintain generous outer safe zones (at least 5% to 8% margin on all sides).
- **Legibility**: Text elements must remain legible at 1080p width or height.
- **Root Container**: Fixed dimensions matching viewport, `#root { width: 100vw; height: 100vh; overflow: hidden; position: relative; }`.
- **CSS Animations**: Must be set to `animation-play-state: paused` or driven exclusively via CSS variables updated in `window.seek(t)`.
