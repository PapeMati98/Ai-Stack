#!/usr/bin/env python3
"""
UAT suite for the html-video architecture flow composition.

Performs genuine headless-Chrome rendering through the unchanged render_video.py
engine into unique retained output directories:
- landscape (16:9) and portrait (9:16) MP4 renders covering the composition's
  full declared timeline, with ffprobe validation,
- manifest verification including the current composition SHA-256 and a
  zero-network/no-page-error isolation audit of BOTH render manifests,
- real DOM geometry audit (node containment horizontal AND vertical, pairwise
  overlap, text overflow, root clipping) across 16:9 / 9:16 / 1:1, at frame
  zero and at the completed end state with populated log lines,
- deterministic-state audit: forward / backward / random seek sequences and a
  fresh independent browser must agree exactly on the pure timeline state,
- completion audit: the last sampled frame (t=duration-1/fps) and t=duration
  present the finished flow (full log prefix, 100%, full counters),
- hostile-label safety in a live browser (no injected markup, no script effects),
- zero network attempts across all sessions,
- contact sheets and scenario expected/actual evidence with SHA-256 hashes.

Complies with workspace rules:
- Unique retained run directories; never deletes, unlinks, or cleans anything.
- All subprocesses go through render_video helpers (CREATE_NO_WINDOW on win32).
- Uses the existing isolation helpers; no --no-sandbox, no package installs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
COMPOSITIONS_DIR = SKILL_ROOT / "compositions"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_architecture_flow import load_and_validate, write_composition
from render_video import (
    compute_sha256,
    find_chrome_binary,
    get_runtime_versions,
    render_pipeline,
    setup_browser_page_isolation,
    verify_probed_video,
)

DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parent / '.test-artifacts' / 'video-uat'

# Renders cover the composition's full declared timeline (duration/fps read
# from the validated example config) so the MP4 provably contains the
# completed flow state, not a mid-timeline cut.

CHROME_LAUNCH_ARGS = [
    "--disable-features=ServiceWorker,SharedWorker",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--no-first-run",
    "--headless=new",
    "--mute-audio",
]

SCENARIOS: list[dict[str, Any]] = []


def record(name: str, expected: str, actual: str, passed: bool, evidence: list[str] | None = None) -> None:
    SCENARIOS.append(
        {
            "name": name,
            "expected": expected,
            "actual": actual,
            "passed": bool(passed),
            "evidence": evidence or [],
        }
    )
    marker = "PASS" if passed else "FAIL"
    print(f"[uat:{marker}] {name}")


def allocate_run_dir(artifact_root: Path) -> Path:
    base = artifact_root / "architecture_flow_uat"
    base.mkdir(parents=True, exist_ok=True)
    idx = 1
    while (base / f"run_{idx:03d}").exists():
        idx += 1
    run_dir = base / f"run_{idx:03d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def build_hostile_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "title": '</script><script>window.__afPwned=1</script>',
        "subtitle": '<img src="https://evil.invalid/x.png" onerror="window.__afPwned=1">',
        "badge": '&<b>"hostile"</b>',
        "telemetry_mode": "illustrative",
        "duration_sec": 4.0,
        "fps": 30,
        "packets_per_edge": 2,
        "grid": {"cols": 2, "rows": 1},
        "nodes": [
            {
                "id": "h1",
                "label": '"cudzysłów" <svg onload=window.__afPwned=1> & <tag> ąćę',
                "kind": "source",
                "col": 0,
                "row": 0,
            },
            {
                "id": "h2",
                "label": "javascript:alert(1) https://evil.invalid/payload łóż",
                "kind": "sink",
                "col": 1,
                "row": 0,
            },
        ],
        "edges": [
            {
                "id": "e-h1-h2",
                "from": "h1",
                "to": "h2",
                "kind": "data",
                "label": '</script><iframe src="https://evil.invalid">',
            }
        ],
        "counters": {"records_target": 100, "error_budget": 1, "queue_max": 10},
        "log_messages": ['<script>window.__afPwned=1</script>', "normalny wpis & <nawiasy>"],
    }


GEOMETRY_JS = r"""
() => {
  const root = document.documentElement;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const problems = [];
  const nodeRects = [];
  document.querySelectorAll('g.flow-node').forEach(g => {
    const r = g.getBoundingClientRect();
    nodeRects.push({ id: g.id, x: r.x, y: r.y, w: r.width, h: r.height });
  });
  for (const n of nodeRects) {
    if (n.x < -0.5 || n.y < -0.5 || n.x + n.w > vw + 0.5 || n.y + n.h > vh + 0.5) {
      problems.push('node outside viewport: ' + n.id);
    }
  }
  for (let i = 0; i < nodeRects.length; i++) {
    for (let j = i + 1; j < nodeRects.length; j++) {
      const a = nodeRects[i];
      const b = nodeRects[j];
      const overlapX = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const overlapY = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      if (overlapX > 0.5 && overlapY > 0.5) {
        problems.push('node overlap: ' + a.id + ' vs ' + b.id);
      }
    }
  }
  document.querySelectorAll('g.flow-node').forEach(g => {
    const nodeRect = g.getBoundingClientRect();
    g.querySelectorAll('text').forEach(t => {
      const tr = t.getBoundingClientRect();
      if (tr.width > nodeRect.width + 1 || tr.height > nodeRect.height + 1) {
        problems.push('text exceeds node box in ' + g.id + ': ' + t.textContent.slice(0, 24));
      }
      if (tr.x < nodeRect.x - 1 || tr.x + tr.width > nodeRect.x + nodeRect.width + 1) {
        problems.push('text horizontally outside node box in ' + g.id);
      }
      if (tr.y < nodeRect.y - 1 || tr.y + tr.height > nodeRect.y + nodeRect.height + 1) {
        problems.push('text vertically outside node box in ' + g.id);
      }
    });
  });
  document.querySelectorAll('svg.flow-diagram text').forEach(t => {
    const tr = t.getBoundingClientRect();
    if (tr.x < -0.5 || tr.y < -0.5 || tr.x + tr.width > vw + 0.5 || tr.y + tr.height > vh + 0.5) {
      problems.push('svg text clipped by viewport: ' + t.textContent.slice(0, 24));
    }
  });
  const clipped = [];
  document.querySelectorAll('.kpi-value, .log-line, .sub-title, .main-title').forEach(elm => {
    if (elm.scrollWidth > elm.clientWidth + 1) {
      clipped.push(elm.id || elm.className);
    }
  });
  const logLineSample = [];
  let logLineNonempty = 0;
  document.querySelectorAll('.log-line').forEach(elm => {
    logLineSample.push(elm.textContent);
    if (elm.textContent.length > 0) { logLineNonempty += 1; }
  });
  function renderedFontPx(selector) {
    const t = document.querySelector(selector);
    if (!t) { return null; }
    const cssPx = parseFloat(window.getComputedStyle(t).fontSize);
    const svg = t.ownerSVGElement;
    if (!svg) { return cssPx; }
    const vbWidth = svg.viewBox && svg.viewBox.baseVal ? svg.viewBox.baseVal.width : 0;
    const drawnWidth = svg.getBoundingClientRect().width;
    const scale = vbWidth > 0 ? drawnWidth / vbWidth : 1;
    return Math.round(cssPx * scale * 100) / 100;
  }
  const labelSizes = {
    nodeLabel: renderedFontPx('.node-label'),
    edgeLabel: renderedFontPx('.edge-label'),
    nodeKind: renderedFontPx('.node-kind')
  };
  return {
    viewport: { w: vw, h: vh },
    nodeCount: nodeRects.length,
    problems: problems,
    htmlClipped: clipped,
    labelSizes: labelSizes,
    logLineSample: logLineSample,
    logLineNonempty: logLineNonempty,
    rootScroll: { w: root.scrollWidth, h: root.scrollHeight }
  };
}
"""

MIN_RENDERED_LABEL_PX = {"nodeLabel": 16.0, "edgeLabel": 13.0, "nodeKind": 11.0}


def open_isolated_page(p: Any, browser_bin: str, composition: Path, aspect: str, vw: int, vh: int) -> tuple[Any, Any, list[str], list[str]]:
    """Launches an isolated browser context with the existing isolation helpers."""
    browser = p.chromium.launch(
        executable_path=browser_bin,
        headless=True,
        args=CHROME_LAUNCH_ARGS,
        chromium_sandbox=True,
    )
    context = browser.new_context(viewport={"width": vw, "height": vh}, service_workers="block")
    page = context.new_page()
    blocked: list[str] = []
    page_errors: list[str] = []
    setup_browser_page_isolation(
        context=context,
        page=page,
        composition_path=composition.resolve(),
        blocked_requests=blocked,
    )
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    file_url = f"file:///{composition.resolve().as_posix()}?aspect={aspect}&w={vw}&h={vh}"
    page.goto(file_url, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_function("() => document.readyState === 'complete'", timeout=15000)
    page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()")
    page.wait_for_function("() => window.seek !== undefined", timeout=15000)
    page.wait_for_function(
        "() => document.documentElement.getAttribute('data-timeline-ready') === 'true'",
        timeout=15000,
    )
    return browser, page, blocked, page_errors


def snapshot_at(page: Any, t: float) -> Any:
    page.evaluate(f"window.seek({t})")
    return page.evaluate("() => window.renderTimeline.snapshot()")


def main() -> int:
    parser = argparse.ArgumentParser(description="UAT suite for the html-video architecture flow composition.")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="Root directory for retained UAT evidence (unique run subdirectories).",
    )
    parser.add_argument("--chrome-path", help="Explicit path to Chrome executable.")
    args = parser.parse_args()

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run_dir = allocate_run_dir(args.artifact_root.resolve())
    print(f"[uat] evidence run dir: {run_dir}")

    # ------------------------------------------------------------------
    # 1. Build compositions under test (example + hostile), retained in run dir
    # ------------------------------------------------------------------
    comp_dir = run_dir / "composition"
    cfg = load_and_validate(COMPOSITIONS_DIR / "architecture_flow.example.json")
    example_comp = write_composition(cfg, comp_dir / "architecture_flow_uat.html")
    render_duration = float(cfg["duration_sec"])
    render_fps = int(cfg["fps"])
    render_frames = int(math.ceil(render_duration * render_fps))
    last_frame_t = (render_frames - 1) / render_fps
    hostile_comp = write_composition(build_hostile_config(), comp_dir / "architecture_flow_hostile.html")
    example_sha = compute_sha256(example_comp)
    record(
        "build compositions from JSON",
        "example and hostile compositions built from validated configs, retained under run dir",
        f"example sha256={example_sha[:16]}…, hostile built",
        example_comp.is_file() and hostile_comp.is_file(),
        [str(example_comp), str(hostile_comp)],
    )

    # ------------------------------------------------------------------
    # 2. Genuine renders through the unchanged render_video.py pipeline
    # ------------------------------------------------------------------
    land_dir = run_dir / "render_16_9"
    res_land = render_pipeline(
        composition=example_comp,
        out_dir=land_dir,
        out_name="architecture_flow_16_9.mp4",
        aspect="16:9",
        duration=render_duration,
        fps=render_fps,
        force=False,
        chrome_path=args.chrome_path,
    )
    probe_land = res_land["probe"]
    try:
        verify_probed_video(
            probe=probe_land,
            expected_w=1920,
            expected_h=1080,
            expected_duration=render_duration,
            expected_fps=render_fps,
            expected_nb_frames=render_frames,
        )
        land_ok = True
        land_err = ""
    except Exception as exc:
        land_ok = False
        land_err = str(exc)
    record(
        "render 16:9 landscape MP4 + ffprobe",
        f"1920x1080, {render_duration}s, {render_fps}fps, {render_frames} frames via ffprobe (full declared timeline)",
        f"probe ok={land_ok} {land_err}",
        land_ok,
        [res_land["output_mp4"], res_land["contact_sheet"], res_land["manifest"]],
    )

    port_dir = run_dir / "render_9_16"
    res_port = render_pipeline(
        composition=example_comp,
        out_dir=port_dir,
        out_name="architecture_flow_9_16.mp4",
        aspect="9:16",
        duration=render_duration,
        fps=render_fps,
        force=False,
        chrome_path=args.chrome_path,
    )
    probe_port = res_port["probe"]
    try:
        verify_probed_video(
            probe=probe_port,
            expected_w=1080,
            expected_h=1920,
            expected_duration=render_duration,
            expected_fps=render_fps,
            expected_nb_frames=render_frames,
        )
        port_ok = True
        port_err = ""
    except Exception as exc:
        port_ok = False
        port_err = str(exc)
    record(
        "render 9:16 portrait MP4 + ffprobe",
        f"1080x1920, {render_duration}s, {render_fps}fps, {render_frames} frames via ffprobe (full declared timeline)",
        f"probe ok={port_ok} {port_err}",
        port_ok,
        [res_port["output_mp4"], res_port["contact_sheet"], res_port["manifest"]],
    )

    det_land = res_land.get("determinism", {})
    det_port = res_port.get("determinism", {})
    record(
        "engine seek-order determinism (both renders)",
        "renderer seek invariance tolerance_passed=true for landscape and portrait",
        f"landscape mae={det_land.get('mae')}, portrait mae={det_port.get('mae')}",
        bool(det_land.get("tolerance_passed")) and bool(det_port.get("tolerance_passed")),
    )

    # ------------------------------------------------------------------
    # 3. Manifest: current composition hash and blocked network audit
    # ------------------------------------------------------------------
    manifest_land = json.loads(Path(res_land["manifest"]).read_text(encoding="utf-8"))
    manifest_comp = manifest_land.get("composition", {})
    manifest_sha = manifest_comp.get("sha256", "")
    current_sha = compute_sha256(example_comp)
    record(
        "manifest records current composition hash",
        f"manifest composition.sha256 == current file sha256 ({current_sha[:16]}…)",
        f"manifest sha256={manifest_sha[:16]}… path match={Path(manifest_comp.get('path','')).resolve() == example_comp.resolve()}",
        manifest_sha == current_sha
        and Path(manifest_comp.get("path", "")).resolve() == example_comp.resolve(),
        [res_land["manifest"]],
    )
    manifest_port = json.loads(Path(res_port["manifest"]).read_text(encoding="utf-8"))
    iso_land = manifest_land.get("isolation_audit", {})
    iso_port = manifest_port.get("isolation_audit", {})
    blocked_land = iso_land.get("blocked_requests_count", -1)
    blocked_port = iso_port.get("blocked_requests_count", -1)
    record(
        "zero network attempts during renders",
        "isolation_audit.blocked_requests_count == 0 and no page/console errors for BOTH renders (landscape and portrait)",
        f"landscape blocked={blocked_land} page_errors={len(iso_land.get('page_errors', []))} "
        f"console_errors={len(iso_land.get('console_errors', []))}; "
        f"portrait blocked={blocked_port} page_errors={len(iso_port.get('page_errors', []))} "
        f"console_errors={len(iso_port.get('console_errors', []))}",
        blocked_land == 0
        and blocked_port == 0
        and not iso_land.get("page_errors")
        and not iso_land.get("console_errors")
        and not iso_port.get("page_errors")
        and not iso_port.get("console_errors"),
        [res_land["manifest"], res_port["manifest"]],
    )

    # ------------------------------------------------------------------
    # 4. Browser-level audits: geometry, deterministic state, hostile safety
    # ------------------------------------------------------------------
    from playwright.sync_api import sync_playwright

    browser_bin = find_chrome_binary(args.chrome_path)
    browser_version = "unknown"
    total_blocked: list[str] = []

    with sync_playwright() as p:
        # 4a. DOM geometry audit across the three supported aspects, at frame
        # zero AND at the completed end state (log lines populated, so the
        # .log-line clip check exercises real log content, not empty divs).
        geometry_results: dict[str, Any] = {}
        geometry_ok = True
        legibility_ok = True
        for aspect, vw, vh in (("16:9", 1920, 1080), ("9:16", 1080, 1920), ("1:1", 1080, 1080)):
            browser, page, blocked, page_errors = open_isolated_page(p, browser_bin, example_comp, aspect, vw, vh)
            browser_version = browser.version
            total_blocked.extend(blocked)
            page.evaluate("window.seek(0.0)")
            geo_zero = page.evaluate(GEOMETRY_JS)
            shot_zero = run_dir / f"frame_zero_{aspect.replace(':', '_')}.png"
            page.screenshot(path=str(shot_zero), type="png")
            page.evaluate(f"window.seek({render_duration})")
            geo_end = page.evaluate(GEOMETRY_JS)
            shot_end = run_dir / f"frame_end_{aspect.replace(':', '_')}.png"
            page.screenshot(path=str(shot_end), type="png")
            merged_problems = list(geo_zero["problems"]) + [
                "end-state: " + prob for prob in geo_end["problems"]
            ]
            merged_clipped = list(geo_zero["htmlClipped"]) + [
                "end-state: " + clip for clip in geo_end["htmlClipped"]
            ]
            geometry_results[aspect] = {
                **geo_end,
                "problems": merged_problems,
                "htmlClipped": merged_clipped,
                "frame_zero": {
                    "problems": geo_zero["problems"],
                    "htmlClipped": geo_zero["htmlClipped"],
                    "labelSizes": geo_zero["labelSizes"],
                },
                "log_lines_populated_at_end": geo_end.get("logLineNonempty", 0),
                "screenshot": str(shot_zero),
                "screenshot_end": str(shot_end),
                "page_errors": page_errors,
            }
            if merged_problems or merged_clipped or page_errors:
                geometry_ok = False
            if geo_end.get("logLineNonempty", 0) == 0 and cfg["log_messages"]:
                # End state must show real log content for the clip check to
                # be meaningful; an empty log here is a failed audit.
                geometry_ok = False
            sizes = geo_end.get("labelSizes") or {}
            for label_kind, floor in MIN_RENDERED_LABEL_PX.items():
                value = sizes.get(label_kind)
                if value is None or value < floor:
                    legibility_ok = False
            browser.close()
        geo_file = run_dir / "geometry_audit.json"
        geo_file.write_text(json.dumps(geometry_results, indent=2), encoding="utf-8")
        geo_shots = []
        for a in geometry_results:
            geo_shots.append(geometry_results[a]["screenshot"])
            geo_shots.append(geometry_results[a]["screenshot_end"])
        record(
            "DOM geometry: no overflow, no overlap, no clipping (16:9/9:16/1:1)",
            "all node boxes inside viewport, no pairwise overlap, text contained horizontally AND vertically "
            "in nodes and viewport, checked at frame zero and at the completed end state with populated log lines",
            "; ".join(
                f"{a}: problems={len(g['problems'])} clipped={len(g['htmlClipped'])} end_log_lines={g['log_lines_populated_at_end']}"
                for a, g in geometry_results.items()
            ),
            geometry_ok,
            geo_shots + [str(geo_file)],
        )
        record(
            "rendered SVG label sizes above legibility floor",
            "rendered px per aspect: node-label >= 16, edge-label >= 13, node-kind >= 11 (computed from computed style x viewBox scale)",
            "; ".join(
                f"{a}: node={g['labelSizes']['nodeLabel']}px edge={g['labelSizes']['edgeLabel']}px kind={g['labelSizes']['nodeKind']}px"
                for a, g in geometry_results.items()
            ),
            legibility_ok,
            [str(geo_file)] + geo_shots,
        )

        # 4b. Deterministic state: forward / backward / random seek + fresh browser
        browser, page, blocked, page_errors = open_isolated_page(p, browser_bin, example_comp, "16:9", 1920, 1080)
        total_blocked.extend(blocked)
        probe_times = [0.0, 0.5, 1.3, 2.7, 3.9]
        forward = {}
        for t in probe_times:
            forward[t] = snapshot_at(page, t)
        backward = {}
        for t in [4.0, 3.1, 2.2, 1.0, 0.4]:
            backward[t] = snapshot_at(page, t)
        random_order = [2.7, 0.4, 3.9, 1.3, 0.5, 2.7, 0.0]
        random_snaps = {}
        for t in random_order:
            random_snaps[t] = snapshot_at(page, t)
        browser.close()

        mismatches = []
        for t in (2.7, 3.9, 1.3, 0.5, 0.0):
            if t in forward and t in random_snaps and forward[t] != random_snaps[t]:
                mismatches.append(f"forward-vs-random at t={t}")
        backward_vs_forward_ok = True
        # backward path never revisits probe times exactly; verify its own repeatability:
        browser, page, blocked, page_errors = open_isolated_page(p, browser_bin, example_comp, "16:9", 1920, 1080)
        total_blocked.extend(blocked)
        backward_repeat = {}
        for t in [4.0, 3.1, 2.2, 1.0, 0.4]:
            backward_repeat[t] = snapshot_at(page, t)
        for t in backward:
            if backward[t] != backward_repeat[t]:
                backward_vs_forward_ok = False
                mismatches.append(f"backward repeat mismatch at t={t}")
        # cross-check common time reached differently: fresh page, direct seek
        fresh_direct = snapshot_at(page, 1.3)
        if fresh_direct != forward[1.3]:
            mismatches.append("fresh-page direct seek vs forward at t=1.3")
        browser.close()

        # Fresh independent browser process repeat
        browser, page, blocked, page_errors = open_isolated_page(p, browser_bin, example_comp, "16:9", 1920, 1080)
        total_blocked.extend(blocked)
        fresh_13 = snapshot_at(page, 1.3)
        fresh_39 = snapshot_at(page, 3.9)
        if fresh_13 != forward[1.3]:
            mismatches.append("fresh browser t=1.3")
        if fresh_39 != forward[3.9]:
            mismatches.append("fresh browser t=3.9")
        frame_zero_state = snapshot_at(page, 0.0)
        browser.close()

        det_file = run_dir / "determinism_states.json"
        det_file.write_text(
            json.dumps(
                {
                    "probe_times": probe_times,
                    "forward": {str(k): v for k, v in forward.items()},
                    "backward": {str(k): v for k, v in backward.items()},
                    "random_order": random_order,
                    "fresh_browser": {"t1.3": fresh_13, "t3.9": fresh_39},
                    "mismatches": mismatches,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        record(
            "deterministic states: forward/backward/random seek + fresh browser",
            "identical pure timeline state regardless of seek order or browser instance",
            f"mismatches={mismatches if mismatches else 'none'}",
            len(mismatches) == 0 and backward_vs_forward_ok,
            [str(det_file)],
        )

        # Frame zero completeness: full diagram visible at t=0
        f0_ok = (
            isinstance(frame_zero_state, dict)
            and len(frame_zero_state.get("edges", [])) == len(cfg["edges"])
            and len(frame_zero_state.get("packets", [])) == len(cfg["edges"]) * cfg["packets_per_edge"]
            and len(frame_zero_state.get("nodes", {})) == len(cfg["nodes"])
            and frame_zero_state.get("progress") == "0%"
        )
        record(
            "frame zero shows the full diagram",
            f"{len(cfg['nodes'])} nodes, {len(cfg['edges'])} edges, all packets present at t=0",
            f"nodes={len(frame_zero_state.get('nodes', {}))} edges={len(frame_zero_state.get('edges', []))} packets={len(frame_zero_state.get('packets', []))}",
            bool(f0_ok),
        )

        # Completed flow state must be visible in the final rendered frame:
        # the engine samples t=i/fps strictly below duration, so the last
        # sampled frame (t=duration-1/fps) and t=duration must both present
        # the finished log prefix, 100% progress and full counters.
        browser, page, blocked, page_errors = open_isolated_page(p, browser_bin, example_comp, "16:9", 1920, 1080)
        total_blocked.extend(blocked)
        snap_last_frame = snapshot_at(page, last_frame_t)
        snap_end = snapshot_at(page, render_duration)
        snap_mid = snapshot_at(page, render_duration / 2)
        comp_shot = run_dir / "completion_state_16_9.png"
        page.evaluate(f"window.seek({last_frame_t})")
        page.screenshot(path=str(comp_shot), type="png")
        browser.close()
        msgs = cfg["log_messages"]
        budget = cfg["counters"]["error_budget"]
        target = cfg["counters"]["records_target"]
        if len(msgs) >= 3:
            expected_tail = msgs[-3:]
        else:
            expected_tail = msgs + [""] * (3 - len(msgs))
        target_txt = f"{target:,} / {target:,}"
        completion_notes: list[str] = []
        for label, snap in (("last-frame", snap_last_frame), ("end", snap_end)):
            if snap.get("progress") != "100%":
                completion_notes.append(f"{label} progress={snap.get('progress')}")
            if snap.get("errors") != str(budget):
                completion_notes.append(f"{label} errors={snap.get('errors')} want {budget}")
            if snap.get("records") != target_txt:
                completion_notes.append(f"{label} records={snap.get('records')} want {target_txt}")
            if list(snap.get("log", [])) != list(expected_tail):
                completion_notes.append(f"{label} log tail mismatch: {snap.get('log')}")
        if snap_mid.get("progress") == "100%" or list(snap_mid.get("log", [])) == list(expected_tail):
            completion_notes.append("mid-timeline already complete (snap window too wide)")
        completion_ok = not completion_notes and not page_errors
        record(
            "completed flow state visible in the final rendered frame",
            "at t=duration-1/fps (last sampled frame) and t=duration: progress=100%, errors=error_budget, "
            "records=target/target, log shows the full final prefix; mid-timeline NOT complete",
            "; ".join(completion_notes)
            if completion_notes
            else (
                f"last-frame t={last_frame_t:.4f}: progress=100%, errors={budget}, "
                f"records={target_txt}, log tail={len(expected_tail)} msgs; "
                f"mid t={render_duration / 2}: progress={snap_mid.get('progress')}"
            ),
            completion_ok,
            [str(comp_shot)],
        )

        # 4c. Hostile-label safety in a live browser
        browser, page, blocked, page_errors = open_isolated_page(p, browser_bin, hostile_comp, "16:9", 1920, 1080)
        total_blocked.extend(blocked)
        for t in (0.0, 1.0, 2.0, 3.0, 4.0):
            page.evaluate(f"window.seek({t})")
        hostile_eval = page.evaluate(
            """() => ({
                pwned: typeof window.__afPwned === 'undefined' ? 'undefined' : 'PWNED',
                scriptCount: document.querySelectorAll('script').length,
                iframes: document.querySelectorAll('iframe').length,
                images: document.querySelectorAll('img').length,
                nodeLabel: document.querySelector('#node-h1 .node-label') ? document.querySelector('#node-h1 .node-label').textContent : null,
                logText: document.querySelector('#log-lines') ? document.querySelector('#log-lines').textContent : null
            })"""
        )
        hostile_shot = run_dir / "hostile_frame.png"
        page.screenshot(path=str(hostile_shot), type="png")
        browser.close()
        expected_label_fragment = "cudzysłów"
        hostile_ok = (
            hostile_eval["pwned"] == "undefined"
            and hostile_eval["scriptCount"] == 2
            and hostile_eval["iframes"] == 0
            and hostile_eval["images"] == 0
            and not page_errors
            and hostile_eval["nodeLabel"] is not None
            and expected_label_fragment in hostile_eval["nodeLabel"]
            and "<script>" in (hostile_eval["logText"] or "")
        )
        hostile_file = run_dir / "hostile_audit.json"
        hostile_file.write_text(json.dumps(hostile_eval, indent=2), encoding="utf-8")
        record(
            "hostile labels inert in live browser",
            "no injected script/iframe/img effects; hostile strings render as literal text",
            f"pwned={hostile_eval['pwned']} scripts={hostile_eval['scriptCount']} "
            f"iframes={hostile_eval['iframes']} imgs={hostile_eval['images']} "
            f"label_literal={expected_label_fragment in (hostile_eval['nodeLabel'] or '')} page_errors={len(page_errors)}",
            bool(hostile_ok),
            [str(hostile_shot), str(hostile_file)],
        )

    record(
        "zero network attempts across all UAT browser sessions",
        "no request ever reached the isolation route layer (blocked list empty)",
        f"total blocked requests recorded: {len(total_blocked)}",
        len(total_blocked) == 0,
    )

    # ------------------------------------------------------------------
    # 5. Evidence manifest with hashes
    # ------------------------------------------------------------------
    evidence_files: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            evidence_files[str(path.relative_to(run_dir)).replace("\\", "/")] = compute_sha256(path)

    report = {
        "uat": "architecture_flow",
        "started_utc": started,
        "run_dir": str(run_dir),
        "composition": {
            "path": str(example_comp.resolve()),
            "sha256": compute_sha256(example_comp),
        },
        "runtime": get_runtime_versions(browser_bin=browser_bin, browser_version=browser_version),
        "scenarios": SCENARIOS,
        "passed": sum(1 for s in SCENARIOS if s["passed"]),
        "failed": sum(1 for s in SCENARIOS if not s["passed"]),
        "evidence_sha256": evidence_files,
    }
    report_path = run_dir / "uat_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[uat] report: {report_path}")
    print(f"[uat] scenarios passed: {report['passed']}, failed: {report['failed']}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
