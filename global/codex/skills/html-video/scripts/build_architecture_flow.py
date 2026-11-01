#!/usr/bin/env python3
"""
JSON-configured architecture flow composition builder for the html-video skill.

Reads a strict, typed JSON flow description (nodes, edges, packets, illustrative
counters and log messages) and emits a self-contained HTML composition that
follows the html-video timeline contract (window.renderTimeline, window.seek,
data-timeline-ready) and renders through the unchanged render_video.py engine.

Safety contract:
- Config is data only. Every string reaches the DOM exclusively via
  textContent / createElementNS; palette and styling come from fixed enums
  inside the template. No arbitrary style/HTML/URL/path fields exist.
- The embedded JSON blob escapes `&`, `<`, `>` and U+2028/U+2029, so closing
  script sequences can never break out of the config block.
- Unknown keys, wrong types, non-finite numbers, duplicate IDs, colliding grid
  positions and invalid edge references are rejected with clear, redacted
  errors: the JSON path and a stable reason are reported, while the raw
  offending key, value, or filesystem path is never echoed.
- Config inputs that are symlinks, junctions, or other reparse points (or that
  live under one) are refused. Outputs are created atomically with
  O_CREAT|O_EXCL; existing outputs are refused without --force, and --force
  only ever rewrites a plain regular file, never an alias. Nothing is removed.
- The diagram is the primary product: the SVG is laid out in rendered pixel
  space with viewport-aware typography, so node and edge labels stay legible
  in 16:9, 9:16, and 1:1 instead of being shrunk by viewBox scaling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

SCHEMA_VERSION = 1

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

NODE_KINDS = ("source", "process", "sink", "store", "gate")
EDGE_KINDS = ("data", "control", "event")
TELEMETRY_MODES = ("illustrative",)

MIN_NODES = 2
MAX_NODES = 24
MIN_EDGES = 1
MAX_EDGES = 48
MAX_LOG_MESSAGES = 12

MAX_TITLE = 120
MAX_SUBTITLE = 200
MAX_BADGE = 60
MAX_NODE_LABEL = 64
MAX_EDGE_LABEL = 48
MAX_LOG_MESSAGE = 120

MIN_DURATION = 1.0
MAX_DURATION = 30.0
MIN_FPS = 12
MAX_FPS = 60
MIN_PACKETS = 1
MAX_PACKETS = 8
MAX_GRID_CELLS = 6

MAX_CONFIG_BYTES = 262_144

_O_BINARY = getattr(os, "O_BINARY", 0)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

TOP_LEVEL_KEYS = {
    "schema_version",
    "title",
    "subtitle",
    "badge",
    "telemetry_mode",
    "duration_sec",
    "fps",
    "packets_per_edge",
    "grid",
    "nodes",
    "edges",
    "counters",
    "log_messages",
}
NODE_KEYS = {"id", "label", "kind", "col", "row"}
EDGE_KEYS = {"id", "from", "to", "kind", "label"}
GRID_KEYS = {"cols", "rows"}
COUNTER_KEYS = {"records_target", "error_budget", "queue_max"}

DEFAULT_COUNTERS = {"records_target": 1000, "error_budget": 5, "queue_max": 100}
DEFAULT_SUBTITLE = "Ilustracyjny przepływ danych między warstwami systemu."
DEFAULT_BADGE = "ILUSTRACYJNE · DANE POKAZOWE"


class ConfigError(ValueError):
    """Raised when the flow config violates the strict schema or is an unsafe input."""


class OutputError(RuntimeError):
    """Raised when the output path is unsafe or would overwrite existing work."""


def _fail(path: str, reason: str) -> None:
    raise ConfigError(f"{path}: {reason}")


def _check_known_keys(obj: dict[str, Any], allowed: set[str], path: str) -> None:
    for key in obj:
        if key not in allowed:
            _fail(path, f"unknown key rejected (allowed keys: {sorted(allowed)})")


def _is_disallowed_char(ch: str) -> bool:
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029)


def _val_str(value: Any, path: str, max_len: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(path, f"must be a string (got {type(value).__name__})")
    if not allow_empty and value.strip() == "":
        _fail(path, "must not be empty")
    if len(value) > max_len:
        _fail(path, f"too long ({len(value)} chars, max {max_len})")
    for ch in value:
        if _is_disallowed_char(ch):
            _fail(path, f"contains a forbidden control character (U+{ord(ch):04X})")
    return value


def _val_int(value: Any, path: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"must be an integer (got {type(value).__name__})")
    if not (lo <= value <= hi):
        _fail(path, f"must be in range [{lo}, {hi}] (got magnitude outside bounds)")
    return value


def _val_float(value: Any, path: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, f"must be a number (got {type(value).__name__})")
    fval = float(value)
    if not math.isfinite(fval):
        _fail(path, "must be a finite number (NaN/Infinity rejected)")
    if not (lo <= fval <= hi):
        _fail(path, f"must be in range [{lo}, {hi}]")
    return fval


def _val_id(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail(path, f"must be a string ID (got {type(value).__name__})")
    if not ID_RE.match(value):
        _fail(path, "must match ^[a-z0-9][a-z0-9_-]{0,31}$ (unsafe or overlong ID rejected)")
    return value


def validate_config(raw: Any) -> dict[str, Any]:
    """Validates the raw parsed JSON against the strict flow schema and returns a normalized config."""
    if not isinstance(raw, dict):
        _fail("$", f"top level must be an object (got {type(raw).__name__})")
    _check_known_keys(raw, TOP_LEVEL_KEYS, "$")

    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        _fail("$.schema_version", "must be an integer")
    if schema_version != SCHEMA_VERSION:
        _fail("$.schema_version", f"unsupported version (expected {SCHEMA_VERSION})")

    telemetry_mode = raw.get("telemetry_mode", "illustrative")
    if telemetry_mode not in TELEMETRY_MODES:
        _fail(
            "$.telemetry_mode",
            f"unsupported mode; only {sorted(TELEMETRY_MODES)} is allowed in v1 "
            "(measured/live telemetry is explicitly out of scope)",
        )

    title = _val_str(raw.get("title"), "$.title", MAX_TITLE) if "title" in raw else None
    if title is None:
        _fail("$.title", "is required")
    subtitle = _val_str(raw.get("subtitle", DEFAULT_SUBTITLE), "$.subtitle", MAX_SUBTITLE)
    badge = _val_str(raw.get("badge", DEFAULT_BADGE), "$.badge", MAX_BADGE)

    duration = _val_float(raw.get("duration_sec", 6.0), "$.duration_sec", MIN_DURATION, MAX_DURATION)
    fps = _val_int(raw.get("fps", 30), "$.fps", MIN_FPS, MAX_FPS)
    packets = _val_int(raw.get("packets_per_edge", 3), "$.packets_per_edge", MIN_PACKETS, MAX_PACKETS)

    grid_raw = raw.get("grid", {"cols": 4, "rows": 3})
    if not isinstance(grid_raw, dict):
        _fail("$.grid", "must be an object")
    _check_known_keys(grid_raw, GRID_KEYS, "$.grid")
    grid = {
        "cols": _val_int(grid_raw.get("cols", 4), "$.grid.cols", 1, MAX_GRID_CELLS),
        "rows": _val_int(grid_raw.get("rows", 3), "$.grid.rows", 1, MAX_GRID_CELLS),
    }

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list):
        _fail("$.nodes", "must be a list")
    if not (MIN_NODES <= len(nodes_raw) <= MAX_NODES):
        _fail("$.nodes", f"must contain between {MIN_NODES} and {MAX_NODES} nodes (got {len(nodes_raw)})")

    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_positions: set[tuple[int, int]] = set()
    for idx, node_raw in enumerate(nodes_raw):
        path = f"$.nodes[{idx}]"
        if not isinstance(node_raw, dict):
            _fail(path, "must be an object")
        _check_known_keys(node_raw, NODE_KEYS, path)
        for required in ("id", "label", "kind", "col", "row"):
            if required not in node_raw:
                _fail(path, f"missing required key '{required}'")
        nid = _val_id(node_raw["id"], f"{path}.id")
        if nid in seen_ids:
            _fail(f"{path}.id", "duplicate node ID rejected")
        seen_ids.add(nid)
        label = _val_str(node_raw["label"], f"{path}.label", MAX_NODE_LABEL)
        kind = node_raw["kind"]
        if kind not in NODE_KINDS:
            _fail(f"{path}.kind", f"must be one of {list(NODE_KINDS)}")
        col = _val_int(node_raw["col"], f"{path}.col", 0, grid["cols"] - 1)
        row = _val_int(node_raw["row"], f"{path}.row", 0, grid["rows"] - 1)
        if (col, row) in seen_positions:
            _fail(path, "grid position collides with another node (overlap rejected)")
        seen_positions.add((col, row))
        nodes.append({"id": nid, "label": label, "kind": kind, "col": col, "row": row})

    edges_raw = raw.get("edges")
    if not isinstance(edges_raw, list):
        _fail("$.edges", "must be a list")
    if not (MIN_EDGES <= len(edges_raw) <= MAX_EDGES):
        _fail("$.edges", f"must contain between {MIN_EDGES} and {MAX_EDGES} edges (got {len(edges_raw)})")

    edges: list[dict[str, Any]] = []
    seen_edge_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for idx, edge_raw in enumerate(edges_raw):
        path = f"$.edges[{idx}]"
        if not isinstance(edge_raw, dict):
            _fail(path, "must be an object")
        _check_known_keys(edge_raw, EDGE_KEYS, path)
        for required in ("id", "from", "to"):
            if required not in edge_raw:
                _fail(path, f"missing required key '{required}'")
        eid = _val_id(edge_raw["id"], f"{path}.id")
        if eid in seen_edge_ids:
            _fail(f"{path}.id", "duplicate edge ID rejected")
        seen_edge_ids.add(eid)
        from_id = _val_id(edge_raw["from"], f"{path}.from")
        to_id = _val_id(edge_raw["to"], f"{path}.to")
        if from_id not in seen_ids:
            _fail(f"{path}.from", "references an unknown node ID")
        if to_id not in seen_ids:
            _fail(f"{path}.to", "references an unknown node ID")
        if from_id == to_id:
            _fail(path, "self-loop edges are not supported in v1")
        if (from_id, to_id) in seen_pairs:
            _fail(path, "duplicate edge between the same ordered node pair rejected")
        seen_pairs.add((from_id, to_id))
        kind = edge_raw.get("kind", "data")
        if kind not in EDGE_KINDS:
            _fail(f"{path}.kind", f"must be one of {list(EDGE_KINDS)}")
        label = None
        if "label" in edge_raw:
            label = _val_str(edge_raw["label"], f"{path}.label", MAX_EDGE_LABEL)
        edges.append({"id": eid, "from": from_id, "to": to_id, "kind": kind, "label": label})

    counters_raw = raw.get("counters", {})
    if not isinstance(counters_raw, dict):
        _fail("$.counters", "must be an object")
    _check_known_keys(counters_raw, COUNTER_KEYS, "$.counters")
    counters = {
        key: _val_int(counters_raw.get(key, DEFAULT_COUNTERS[key]), f"$.counters.{key}", 0, 1_000_000)
        for key in ("records_target", "error_budget", "queue_max")
    }

    log_raw = raw.get("log_messages", [])
    if not isinstance(log_raw, list):
        _fail("$.log_messages", "must be a list")
    if len(log_raw) > MAX_LOG_MESSAGES:
        _fail("$.log_messages", f"too many entries (max {MAX_LOG_MESSAGES}, got {len(log_raw)})")
    log_messages = [
        _val_str(msg, f"$.log_messages[{i}]", MAX_LOG_MESSAGE) for i, msg in enumerate(log_raw)
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "subtitle": subtitle,
        "badge": badge,
        "telemetry_mode": "illustrative",
        "duration_sec": duration,
        "fps": fps,
        "packets_per_edge": packets,
        "grid": grid,
        "nodes": nodes,
        "edges": edges,
        "counters": counters,
        "log_messages": log_messages,
    }


def json_for_script(config: dict[str, Any]) -> str:
    """
    Serializes config to a JSON blob safe for embedding inside a
    <script type="application/json"> block: escapes &, <, > and the Unicode
    line/paragraph separators so no closing script sequence can ever appear.
    The result remains valid JSON (round-trips through JSON.parse).
    """
    blob = json.dumps(config, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    out: list[str] = []
    for ch in blob:
        if ch == "<":
            out.append("\\u003c")
        elif ch == ">":
            out.append("\\u003e")
        elif ch == "&":
            out.append("\\u0026")
        elif ch == "\u2028":
            out.append("\\u2028")
        elif ch == "\u2029":
            out.append("\\u2029")
        else:
            out.append(ch)
    return "".join(out)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Architecture Flow (Illustrative)</title>
  <style>
    /* Reset & Base */
    *, *::before, *::after {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    :root {
      --bg-base: #080a0f;
      --bg-card: rgba(18, 22, 34, 0.75);
      --bg-card-border: rgba(56, 189, 248, 0.2);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #475569;
      --accent-cyan: #00f0ff;
      --accent-violet: #a855f7;
      --accent-emerald: #10b981;
      --accent-amber: #f59e0b;
      --glow-cyan: 0 0 25px rgba(0, 240, 255, 0.35);
    }

    html, body {
      width: 100%;
      height: 100%;
      overflow: hidden;
      background-color: var(--bg-base);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      color: var(--text-main);
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    .viewport-container {
      width: 100vw;
      height: 100vh;
      display: flex;
      flex-direction: column;
      position: relative;
      background:
        radial-gradient(circle at 15% 20%, rgba(0, 240, 255, 0.08) 0%, transparent 40%),
        radial-gradient(circle at 85% 75%, rgba(168, 85, 247, 0.08) 0%, transparent 45%),
        linear-gradient(to bottom, #090c14, #05070a);
    }

    .grid-lines {
      position: absolute;
      inset: 0;
      background-size: 60px 60px;
      background-image:
        linear-gradient(to right, rgba(255, 255, 255, 0.02) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(255, 255, 255, 0.02) 1px, transparent 1px);
      pointer-events: none;
    }

    .content-frame {
      position: relative;
      z-index: 10;
      height: 100%;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }

    body.aspect-16-9 .content-frame { padding: 64px 80px; }
    body.aspect-9-16 .content-frame { padding: 80px 64px; }
    body.aspect-1-1 .content-frame { padding: 48px; }

    /* Header */
    .header-block {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .badge-row {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .live-indicator {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      border-radius: 9999px;
      background: rgba(245, 158, 11, 0.12);
      border: 1px solid rgba(245, 158, 11, 0.4);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--accent-amber);
    }

    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent-amber);
      box-shadow: 0 0 10px var(--accent-amber);
    }

    .system-tag {
      font-size: 13px;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--text-dim);
    }

    .main-title {
      font-weight: 800;
      letter-spacing: -0.02em;
      line-height: 1.1;
      background: linear-gradient(135deg, #ffffff 30%, #94a3b8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    body.aspect-16-9 .main-title { font-size: 44px; }
    body.aspect-9-16 .main-title { font-size: 40px; }
    body.aspect-1-1 .main-title { font-size: 34px; }

    .sub-title {
      font-weight: 400;
      color: var(--text-muted);
      line-height: 1.4;
      max-width: 760px;
    }

    body.aspect-16-9 .sub-title { font-size: 19px; }
    body.aspect-9-16 .sub-title { font-size: 19px; line-height: 1.45; max-width: 920px; }
    body.aspect-1-1 .sub-title { font-size: 16px; }

    body.aspect-9-16 .live-indicator { font-size: 12px; padding: 5px 12px; }
    body.aspect-9-16 .system-tag { font-size: 12px; }
    body.aspect-1-1 .live-indicator { font-size: 11px; padding: 4px 10px; }
    body.aspect-1-1 .system-tag { font-size: 11px; }

    /* Main layout */
    .body-layout {
      flex: 1;
      display: flex;
      margin: 30px 0;
      gap: 36px;
      min-height: 0;
    }

    body.aspect-16-9 .body-layout { flex-direction: row; align-items: stretch; }
    body.aspect-9-16 .body-layout { flex-direction: column; gap: 22px; margin: 24px 0; }
    body.aspect-1-1 .body-layout { flex-direction: column; gap: 16px; margin: 18px 0; }

    .metrics-col {
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 18px;
    }

    body.aspect-16-9 .metrics-col { width: 34%; }
    body.aspect-9-16 .metrics-col { width: 100%; gap: 16px; justify-content: flex-start; }
    body.aspect-1-1 .metrics-col { width: 100%; gap: 14px; justify-content: flex-start; }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 16px;
    }

    body.aspect-9-16 .kpi-grid { grid-template-columns: repeat(4, 1fr); gap: 14px; }
    body.aspect-1-1 .kpi-grid { grid-template-columns: repeat(4, 1fr); gap: 12px; }

    .kpi-card {
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 16px;
      padding: 20px 20px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      position: relative;
      overflow: hidden;
      backdrop-filter: blur(12px);
    }

    body.aspect-9-16 .kpi-card { padding: 14px 16px; border-radius: 14px; gap: 4px; }
    body.aspect-1-1 .kpi-card { padding: 12px 14px; border-radius: 14px; gap: 3px; }

    .kpi-card::before {
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--accent-cyan), transparent);
      opacity: 0.6;
    }

    .kpi-label {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-muted);
    }

    body.aspect-9-16 .kpi-label { font-size: 13px; }
    body.aspect-1-1 .kpi-label { font-size: 11.5px; }

    .kpi-value {
      font-size: 30px;
      font-weight: 800;
      letter-spacing: -0.02em;
      color: #ffffff;
      font-variant-numeric: tabular-nums;
    }

    body.aspect-9-16 .kpi-value { font-size: 28px; }
    body.aspect-1-1 .kpi-value { font-size: 23px; }

    .kpi-sub {
      font-size: 12px;
      color: var(--accent-amber);
      letter-spacing: 0.04em;
    }

    body.aspect-9-16 .kpi-sub { font-size: 13px; }
    body.aspect-1-1 .kpi-sub { font-size: 12px; }

    /* Log card */
    .log-card {
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 16px;
      padding: 16px 20px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      backdrop-filter: blur(12px);
      min-height: 118px;
    }

    body.aspect-9-16 .log-card { padding: 12px 18px; min-height: 0; gap: 6px; }
    body.aspect-1-1 .log-card { padding: 10px 16px; min-height: 0; gap: 5px; }

    .log-title {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-muted);
    }

    body.aspect-9-16 .log-title { font-size: 14px; }
    body.aspect-1-1 .log-title { font-size: 12px; }

    .log-line {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      color: var(--text-main);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-height: 17px;
    }

    body.aspect-9-16 .log-line { font-size: 16px; min-height: 20px; }
    body.aspect-1-1 .log-line { font-size: 14px; min-height: 18px; }

    /* Diagram card */
    .chart-col {
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
      min-width: 0;
      min-height: 0;
    }

    .chart-card {
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 20px;
      padding: 24px 24px 20px 24px;
      display: flex;
      flex-direction: column;
      height: 100%;
      min-height: 240px;
      position: relative;
      backdrop-filter: blur(12px);
    }

    body.aspect-9-16 .chart-card { padding: 22px 22px 18px 22px; min-height: 0; }
    body.aspect-1-1 .chart-card { padding: 16px 16px 14px 16px; min-height: 0; }

    .chart-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }

    .chart-title {
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--text-main);
    }

    body.aspect-9-16 .chart-title { font-size: 19px; }
    body.aspect-1-1 .chart-title { font-size: 15px; }

    .chart-legend {
      display: flex;
      gap: 14px;
      font-size: 12px;
      color: var(--text-muted);
      flex-wrap: wrap;
    }

    body.aspect-9-16 .chart-legend { font-size: 15px; gap: 14px; }
    body.aspect-1-1 .chart-legend { font-size: 12.5px; gap: 12px; }

    .legend-item {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .legend-bullet {
      width: 10px;
      height: 10px;
      border-radius: 50%;
    }

    body.aspect-9-16 .legend-bullet { width: 11px; height: 11px; }

    .chart-viewport {
      flex: 1;
      width: 100%;
      height: 100%;
      position: relative;
      min-height: 0;
    }

    svg.flow-diagram {
      width: 100%;
      height: 100%;
      display: block;
    }

    .node-rect {
      fill: rgba(18, 22, 34, 0.9);
      stroke-width: 1.2;
    }

    .node-kind {
      font-weight: 700;
      letter-spacing: 0.16em;
    }

    .node-label {
      fill: var(--text-main);
      font-weight: 600;
    }

    .edge-label {
      fill: var(--text-muted);
      font-weight: 500;
      letter-spacing: 0.02em;
    }

    /* Footer */
    .timeline-footer {
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding-top: 18px;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
    }

    body.aspect-9-16 .timeline-footer { padding-top: 14px; gap: 10px; }
    body.aspect-1-1 .timeline-footer { padding-top: 12px; gap: 9px; }

    .footer-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 14px;
      color: var(--text-muted);
    }

    body.aspect-9-16 .footer-meta { font-size: 17px; }
    body.aspect-1-1 .footer-meta { font-size: 14px; }

    .timecode-display {
      color: var(--accent-cyan);
      font-weight: 700;
    }

    .progress-track {
      width: 100%;
      height: 6px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 9999px;
      overflow: hidden;
      position: relative;
    }

    body.aspect-9-16 .progress-track { height: 8px; }

    .progress-fill {
      position: absolute;
      top: 0;
      left: 0;
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--accent-cyan), var(--accent-violet));
      border-radius: 9999px;
      box-shadow: 0 0 10px rgba(0, 240, 255, 0.5);
    }
  </style>
</head>
<body class="aspect-16-9">
  <div class="viewport-container">
    <div class="grid-lines"></div>

    <div class="content-frame">
      <header class="header-block">
        <div class="badge-row">
          <div class="live-indicator">
            <span class="pulse-dot"></span>
            <span id="badge-text">ILUSTRACYJNE</span>
          </div>
          <span class="system-tag">architecture flow · wartości pokazowe, nie telemetria produkcyjna</span>
        </div>
        <h1 class="main-title" id="flow-title">Przepływ architektury</h1>
        <p class="sub-title" id="flow-subtitle"></p>
      </header>

      <div class="body-layout">
        <div class="metrics-col">
          <div class="kpi-grid">
            <div class="kpi-card">
              <span class="kpi-label">Ilustr. rekordy przetworzone</span>
              <span class="kpi-value" id="kpi-records">0</span>
              <span class="kpi-sub">licznik pokazowy</span>
            </div>
            <div class="kpi-card">
              <span class="kpi-label">Ilustr. błędy</span>
              <span class="kpi-value" id="kpi-errors">0</span>
              <span class="kpi-sub">budżet pokazowy</span>
            </div>
            <div class="kpi-card">
              <span class="kpi-label">Ilustr. kolejka</span>
              <span class="kpi-value" id="kpi-queue">0</span>
              <span class="kpi-sub">stan pokazowy</span>
            </div>
            <div class="kpi-card">
              <span class="kpi-label">Postęp osi czasu</span>
              <span class="kpi-value" id="kpi-progress">0%</span>
              <span class="kpi-sub">deterministyczny</span>
            </div>
          </div>

          <div class="log-card">
            <span class="log-title">Dziennik przepływu (ilustracyjny)</span>
            <div id="log-lines"></div>
          </div>
        </div>

        <div class="chart-col">
          <div class="chart-card">
            <div class="chart-header">
              <span class="chart-title">Diagram przepływu</span>
              <div class="chart-legend" id="diagram-legend"></div>
            </div>
            <div class="chart-viewport" id="diagram-viewport"></div>
          </div>
        </div>
      </div>

      <footer class="timeline-footer">
        <div class="progress-track">
          <div class="progress-fill" id="timeline-progress-bar"></div>
        </div>
        <div class="footer-meta">
          <span id="frame-counter-display">FRAME: 000000 / 000000</span>
          <span class="timecode-display" id="timecode-display">00:00.000</span>
          <span id="fps-badge">DETERMINISTYCZNY SEEK [ILUSTRACYJNY]</span>
        </div>
      </footer>
    </div>
  </div>

  <script id="af-config" type="application/json">__AF_CONFIG_JSON__</script>
  <script>
    (function () {
      'use strict';
      var SVG_NS = 'http://www.w3.org/2000/svg';
      var CFG = JSON.parse(document.getElementById('af-config').textContent);

      var params = new URLSearchParams(window.location.search);
      var aspect = params.get('aspect') || '16:9';
      var defaultW = aspect === '9:16' ? 1080 : (aspect === '1:1' ? 1080 : 1920);
      var defaultH = aspect === '9:16' ? 1920 : 1080;
      var width = parseInt(params.get('w') || String(defaultW), 10);
      var height = parseInt(params.get('h') || String(defaultH), 10);

      if (aspect === '9:16') {
        document.body.className = 'aspect-9-16';
      } else if (aspect === '1:1') {
        document.body.className = 'aspect-1-1';
      } else {
        document.body.className = 'aspect-16-9';
      }

      var DURATION = CFG.duration_sec;
      var FPS = CFG.fps;
      var TOTAL_FRAMES = Math.round(DURATION * FPS);
      var TRAVERSALS = 3;

      var NODE_COLORS = {
        source: '#00f0ff',
        process: '#a855f7',
        sink: '#10b981',
        store: '#f59e0b',
        gate: '#38bdf8'
      };
      var EDGE_COLORS = { data: '#00f0ff', control: '#a855f7', event: '#10b981' };
      var EDGE_DASH = { data: '', control: '7 5', event: '2 6' };
      var KIND_LABELS = {
        source: 'ŹRÓDŁO',
        process: 'PROCES',
        sink: 'UJŚCIE',
        store: 'MAGAZYN',
        gate: 'BRAMKA'
      };

      document.getElementById('badge-text').textContent = CFG.badge;
      document.getElementById('flow-title').textContent = CFG.title;
      document.getElementById('flow-subtitle').textContent = CFG.subtitle;

      var COLS = CFG.grid.cols;
      var ROWS = CFG.grid.rows;

      // Viewport-aware layout: the diagram is the primary product, so the SVG
      // is laid out directly in rendered pixel space of the card viewport.
      // viewBox == container pixels keeps the scale at 1 and font sizes are
      // computed in real pixels, which keeps labels legible in every aspect.
      var host = document.getElementById('diagram-viewport');
      var vbW = Math.max(320, Math.floor(host.clientWidth) || 640);
      var vbH = Math.max(240, Math.floor(host.clientHeight) || 480);

      var cellW = vbW / COLS;
      var cellH = vbH / ROWS;
      var NODE_W = Math.min(260, Math.floor(cellW * 0.8));
      var NODE_H = Math.min(150, Math.floor(cellH * 0.66));
      var F_NODE = Math.max(16, Math.min(26, Math.floor(Math.min(cellW / 12, cellH / 7))));
      var F_EDGE = Math.max(14, F_NODE - 4);
      var F_KIND = Math.max(12, F_NODE - 7);
      var LINE_NODE = Math.round(F_NODE * 1.22);
      var LINE_EDGE = Math.round(F_EDGE * 1.2);
      var PACKET_R = Math.max(4.5, Math.round(F_NODE * 3) / 10);

      var nodeMaxChars = Math.max(8, Math.floor((NODE_W - 20) / (F_NODE * 0.56)));
      var nodeMaxLines = Math.max(1, Math.min(3, Math.floor((NODE_H - F_KIND - 16) / LINE_NODE)));

      var nodeById = {};
      CFG.nodes.forEach(function (n) { nodeById[n.id] = n; });

      function nodeCenter(n) {
        return { x: (n.col + 0.5) * cellW, y: (n.row + 0.5) * cellH };
      }

      function wrapLabel(text, maxChars, maxLines) {
        var words = String(text).split(/\s+/).filter(function (w) { return w.length > 0; });
        var lines = [];
        var cur = '';
        for (var wi = 0; wi < words.length; wi++) {
          var w = words[wi];
          while (w.length > maxChars) {
            if (cur !== '') { lines.push(cur); cur = ''; }
            lines.push(w.slice(0, maxChars - 1) + '-');
            w = w.slice(maxChars - 1);
          }
          if (cur === '') {
            cur = w;
          } else if ((cur + ' ' + w).length <= maxChars) {
            cur = cur + ' ' + w;
          } else {
            lines.push(cur);
            cur = w;
          }
        }
        if (cur !== '') { lines.push(cur); }
        if (lines.length === 0) { lines.push(''); }
        if (lines.length > maxLines) {
          lines = lines.slice(0, maxLines);
          lines[maxLines - 1] = lines[maxLines - 1].slice(0, Math.max(1, maxChars - 1)) + '…';
        }
        return lines;
      }

      function el(name, attrs) {
        var node = document.createElementNS(SVG_NS, name);
        if (attrs) {
          for (var key in attrs) { node.setAttribute(key, attrs[key]); }
        }
        return node;
      }

      var svg = el('svg', {
        'class': 'flow-diagram',
        viewBox: '0 0 ' + vbW + ' ' + vbH,
        preserveAspectRatio: 'xMidYMid meet',
        role: 'img'
      });

      var defs = el('defs');
      Object.keys(EDGE_COLORS).forEach(function (kind) {
        var marker = el('marker', {
          id: 'arrow-' + kind,
          viewBox: '0 0 10 10',
          refX: '9',
          refY: '5',
          markerWidth: '7',
          markerHeight: '7',
          orient: 'auto-start-reverse'
        });
        marker.appendChild(el('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: EDGE_COLORS[kind] }));
        defs.appendChild(marker);
      });
      svg.appendChild(defs);

      var edgeLayer = el('g', { 'class': 'edge-layer' });
      var nodeLayer = el('g', { 'class': 'node-layer' });
      var packetLayer = el('g', { 'class': 'packet-layer' });
      svg.appendChild(edgeLayer);
      svg.appendChild(nodeLayer);
      svg.appendChild(packetLayer);

      var fitTargets = [];

      function rectReach(ux, uy) {
        var tx = ux !== 0 ? (NODE_W / 2 + 8) / Math.abs(ux) : Infinity;
        var ty = uy !== 0 ? (NODE_H / 2 + 8) / Math.abs(uy) : Infinity;
        return Math.min(tx, ty);
      }

      var edgeViews = [];
      CFG.edges.forEach(function (e) {
        var a = nodeCenter(nodeById[e.from]);
        var b = nodeCenter(nodeById[e.to]);
        var dx = b.x - a.x;
        var dy = b.y - a.y;
        var len = Math.sqrt(dx * dx + dy * dy) || 1;
        var ux = dx / len;
        var uy = dy / len;
        var reach = rectReach(ux, uy);
        var x1 = a.x + ux * reach;
        var y1 = a.y + uy * reach;
        var x2 = b.x - ux * reach;
        var y2 = b.y - uy * reach;
        var lineLen = Math.max(40, len - 2 * reach);

        var line = el('line', {
          x1: x1.toFixed(2),
          y1: y1.toFixed(2),
          x2: x2.toFixed(2),
          y2: y2.toFixed(2),
          stroke: EDGE_COLORS[e.kind],
          'stroke-width': '2.2',
          'stroke-opacity': '0.4',
          'marker-end': 'url(#arrow-' + e.kind + ')'
        });
        if (EDGE_DASH[e.kind]) { line.setAttribute('stroke-dasharray', EDGE_DASH[e.kind]); }
        edgeLayer.appendChild(line);

        if (e.label) {
          var mx = (x1 + x2) / 2;
          var my = (y1 + y2) / 2;
          var lx = mx + (-uy) * (F_EDGE + 8);
          var ly = my + ux * (F_EDGE + 8);
          var edgeMaxChars = Math.max(14, Math.min(24, Math.floor((lineLen + cellW * 0.5) / (F_EDGE * 0.55))));
          var labelText = el('text', {
            x: lx.toFixed(2),
            y: ly.toFixed(2),
            'class': 'edge-label',
            'font-size': String(F_EDGE),
            'text-anchor': 'middle'
          });
          wrapLabel(e.label, edgeMaxChars, 2).forEach(function (ln, li) {
            var span = el('tspan', { x: lx.toFixed(2), dy: li === 0 ? '0' : String(LINE_EDGE) });
            span.textContent = ln;
            labelText.appendChild(span);
          });
          edgeLayer.appendChild(labelText);
          fitTargets.push({ el: labelText, maxWidth: Math.max(90, (lineLen + cellW * 0.5) * 0.95), minSize: 10 });
        }

        var packets = [];
        for (var pi = 0; pi < CFG.packets_per_edge; pi++) {
          var c = el('circle', {
            r: String(PACKET_R),
            fill: EDGE_COLORS[e.kind],
            'fill-opacity': '0.45',
            stroke: '#ffffff',
            'stroke-width': '1.2'
          });
          packetLayer.appendChild(c);
          packets.push(c);
        }

        edgeViews.push({ def: e, line: line, x1: x1, y1: y1, x2: x2, y2: y2, packets: packets });
      });

      var nodeViews = {};
      CFG.nodes.forEach(function (n) {
        var c = nodeCenter(n);
        var g = el('g', { 'class': 'flow-node', id: 'node-' + n.id });
        var rect = el('rect', {
          x: (c.x - NODE_W / 2).toFixed(2),
          y: (c.y - NODE_H / 2).toFixed(2),
          width: String(NODE_W),
          height: String(NODE_H),
          rx: '14',
          'class': 'node-rect',
          stroke: NODE_COLORS[n.kind]
        });
        g.appendChild(rect);

        var kindBaseline = c.y - NODE_H / 2 + F_KIND + 6;
        var kindTag = el('text', {
          x: c.x.toFixed(2),
          y: kindBaseline.toFixed(2),
          'class': 'node-kind',
          'font-size': String(F_KIND),
          'text-anchor': 'middle',
          fill: NODE_COLORS[n.kind]
        });
        kindTag.textContent = KIND_LABELS[n.kind];
        g.appendChild(kindTag);
        fitTargets.push({ el: kindTag, maxWidth: NODE_W - 24, minSize: 9 });

        var lines = wrapLabel(n.label, nodeMaxChars, nodeMaxLines);
        var areaTop = kindBaseline + 6;
        var areaBottom = c.y + NODE_H / 2 - 8;
        var blockH = lines.length * LINE_NODE;
        var startBase = areaTop + Math.max(0, (areaBottom - areaTop - blockH) / 2) + F_NODE * 0.85;
        var label = el('text', {
          x: c.x.toFixed(2),
          y: startBase.toFixed(2),
          'class': 'node-label',
          'font-size': String(F_NODE),
          'text-anchor': 'middle'
        });
        lines.forEach(function (ln, li) {
          var span = el('tspan', { x: c.x.toFixed(2), dy: li === 0 ? '0' : String(LINE_NODE) });
          span.textContent = ln;
          label.appendChild(span);
        });
        g.appendChild(label);
        fitTargets.push({ el: label, maxWidth: NODE_W - 16, minSize: 11 });
        nodeLayer.appendChild(g);
        nodeViews[n.id] = { def: n, rect: rect, group: g };
      });

      document.getElementById('diagram-viewport').appendChild(svg);

      var legendHost = document.getElementById('diagram-legend');
      var seenKinds = [];
      CFG.nodes.forEach(function (n) {
        if (seenKinds.indexOf(n.kind) < 0) { seenKinds.push(n.kind); }
      });
      seenKinds.forEach(function (k) {
        var item = document.createElement('div');
        item.className = 'legend-item';
        var bullet = document.createElement('span');
        bullet.className = 'legend-bullet';
        bullet.style.background = NODE_COLORS[k];
        var txt = document.createElement('span');
        txt.textContent = KIND_LABELS[k].toLowerCase();
        item.appendChild(bullet);
        item.appendChild(txt);
        legendHost.appendChild(item);
      });

      var logDivs = [];
      var logHost = document.getElementById('log-lines');
      for (var li2 = 0; li2 < 3; li2++) {
        var d = document.createElement('div');
        d.className = 'log-line';
        d.textContent = '';
        logHost.appendChild(d);
        logDivs.push(d);
      }

      var kpiRecords = document.getElementById('kpi-records');
      var kpiErrors = document.getElementById('kpi-errors');
      var kpiQueue = document.getElementById('kpi-queue');
      var kpiProgress = document.getElementById('kpi-progress');
      var timecodeDisplay = document.getElementById('timecode-display');
      var frameCounterDisplay = document.getElementById('frame-counter-display');
      var progressBar = document.getElementById('timeline-progress-bar');

      function easeInOutCubic(x) {
        return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
      }

      function formatTime(seconds) {
        var m = Math.floor(seconds / 60);
        var s = Math.floor(seconds % 60);
        var ms = Math.floor((seconds % 1) * 1000);
        return String(m).padStart(2, '0') + ':' +
               String(s).padStart(2, '0') + '.' +
               String(ms).padStart(3, '0');
      }

      function seek(t) {
        var clampedT = Math.max(0, Math.min(t, DURATION));
        // The renderer samples frames at t = i/fps, so the last sampled frame
        // is strictly before duration. Any seek inside the final frame
        // interval snaps to the completed flow state (full log prefix, 100%,
        // full counters); the mapping stays a pure function of t, so
        // forward/backward/random seeks and fresh browsers still agree.
        var endSnapT = DURATION - 1 / FPS;
        var progress = endSnapT > 0 && clampedT >= endSnapT - 1e-9 ? 1 : clampedT / DURATION;
        var eased = easeInOutCubic(progress);

        var edgeCount = edgeViews.length;
        var activeIdx = progress >= 1 ? edgeCount - 1 : Math.min(edgeCount - 1, Math.floor(progress * edgeCount));

        var traversalBase = progress * TRAVERSALS;
        for (var ei = 0; ei < edgeCount; ei++) {
          var ev = edgeViews[ei];
          var isActive = ei === activeIdx;
          var isPast = ei < activeIdx;
          ev.line.setAttribute('stroke-width', isActive ? '4.2' : '2.2');
          ev.line.setAttribute('stroke-opacity', isActive ? '0.95' : (isPast ? '0.75' : '0.4'));
          for (var pi = 0; pi < ev.packets.length; pi++) {
            var phase = (traversalBase + pi / ev.packets.length) % 1;
            var cx = ev.x1 + (ev.x2 - ev.x1) * phase;
            var cy = ev.y1 + (ev.y2 - ev.y1) * phase;
            var pk = ev.packets[pi];
            pk.setAttribute('cx', cx.toFixed(2));
            pk.setAttribute('cy', cy.toFixed(2));
            pk.setAttribute('fill-opacity', isActive ? '0.95' : '0.45');
          }
        }

        var activeEdge = CFG.edges[activeIdx];
        for (var nid in nodeViews) {
          var nv = nodeViews[nid];
          var involved = activeEdge !== undefined && (activeEdge.from === nid || activeEdge.to === nid);
          nv.rect.setAttribute('stroke-width', involved ? '2.6' : '1.2');
          nv.group.setAttribute('opacity', involved ? '1' : '0.88');
        }

        var msgs = CFG.log_messages;
        var visibleCount = msgs.length === 0 ? 0 : Math.min(msgs.length, Math.floor(progress * msgs.length));
        var start = Math.max(0, visibleCount - logDivs.length);
        for (var ld = 0; ld < logDivs.length; ld++) {
          var mi = start + ld;
          logDivs[ld].textContent = mi < visibleCount ? msgs[mi] : '';
        }

        var counters = CFG.counters;
        kpiRecords.textContent = Math.round(eased * counters.records_target).toLocaleString('en-US') +
          ' / ' + counters.records_target.toLocaleString('en-US');
        kpiErrors.textContent = String(Math.floor(progress * counters.error_budget));
        kpiQueue.textContent = String(Math.round(counters.queue_max * 4 * progress * (1 - progress)));
        kpiProgress.textContent = Math.floor(progress * 100) + '%';

        progressBar.style.width = (progress * 100).toFixed(2) + '%';
        timecodeDisplay.textContent = formatTime(clampedT) + ' / ' + formatTime(DURATION);
        var currentFrame = Math.min(TOTAL_FRAMES, Math.round(clampedT * FPS));
        frameCounterDisplay.textContent = 'FRAME: ' +
          String(currentFrame).padStart(6, '0') + ' / ' +
          String(TOTAL_FRAMES).padStart(6, '0');
      }

      function snapshot() {
        var packets = [];
        var edges = [];
        for (var ei = 0; ei < edgeViews.length; ei++) {
          var ev = edgeViews[ei];
          edges.push([
            ev.line.getAttribute('stroke-width'),
            ev.line.getAttribute('stroke-opacity')
          ]);
          for (var pi = 0; pi < ev.packets.length; pi++) {
            packets.push([
              ev.packets[pi].getAttribute('cx'),
              ev.packets[pi].getAttribute('cy'),
              ev.packets[pi].getAttribute('fill-opacity')
            ]);
          }
        }
        var nodes = {};
        for (var nid in nodeViews) {
          nodes[nid] = [
            nodeViews[nid].rect.getAttribute('stroke-width'),
            nodeViews[nid].group.getAttribute('opacity')
          ];
        }
        return {
          records: kpiRecords.textContent,
          errors: kpiErrors.textContent,
          queue: kpiQueue.textContent,
          progress: kpiProgress.textContent,
          bar: progressBar.style.width,
          timecode: timecodeDisplay.textContent,
          frame: frameCounterDisplay.textContent,
          edges: edges,
          packets: packets,
          nodes: nodes,
          log: logDivs.map(function (d) { return d.textContent; })
        };
      }

      window.renderTimeline = {
        duration: DURATION,
        fps: FPS,
        width: width,
        height: height,
        aspectRatio: aspect,
        assets: [],
        seek: seek,
        snapshot: snapshot
      };
      window.seek = seek;

      seek(0.0);

      Promise.all([
        document.fonts ? document.fonts.ready : Promise.resolve(),
        new Promise(function (resolve) {
          if (document.readyState === 'complete') {
            resolve();
          } else {
            window.addEventListener('load', resolve);
          }
        })
      ]).then(function () {
        // Final label fit pass with real font metrics, before the readiness
        // marker, so geometry audits observe the definitive rendered sizes.
        for (var fi = 0; fi < fitTargets.length; fi++) {
          var ft = fitTargets[fi];
          var size = parseFloat(ft.el.getAttribute('font-size'));
          var guard = 32;
          while (guard > 0 && size > ft.minSize) {
            var tspans = ft.el.getElementsByTagName('tspan');
            var tooWide = false;
            if (tspans.length === 0) {
              tooWide = ft.el.getComputedTextLength() > ft.maxWidth;
            } else {
              for (var ti = 0; ti < tspans.length; ti++) {
                if (tspans[ti].getComputedTextLength() > ft.maxWidth) { tooWide = true; break; }
              }
            }
            if (!tooWide) { break; }
            size -= 1;
            ft.el.setAttribute('font-size', String(size));
            guard -= 1;
          }
        }
        document.documentElement.setAttribute('data-timeline-ready', 'true');
      });
    })();
  </script>
</body>
</html>
"""

CONFIG_TOKEN = "__AF_CONFIG_JSON__"


def build_html(config: dict[str, Any]) -> str:
    """Builds the final self-contained composition HTML from a validated config."""
    blob = json_for_script(config)
    if TEMPLATE.count(CONFIG_TOKEN) != 1:
        raise RuntimeError("Internal template error: config token missing or duplicated.")
    return TEMPLATE.replace(CONFIG_TOKEN, blob, 1)


def _contains_reparse_point(path: Path) -> bool:
    """True when the path or any existing ancestor is a symlink/junction/reparse point."""
    for cand in (path, *path.parents):
        try:
            st = os.lstat(cand)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return True
        if getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    return False


def load_and_validate(config_path: Path) -> dict[str, Any]:
    """Reads and validates a config file; refuses reparse-point inputs; never echoes paths."""
    if _contains_reparse_point(config_path):
        raise ConfigError("config input is or lives under a reparse point (symlink/junction); refusing unsafe alias (path redacted)")
    try:
        st = os.lstat(config_path)
    except OSError:
        raise ConfigError("config file not found (path redacted)")
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError("config input is not a regular file (path redacted)")
    try:
        fd = os.open(str(config_path), os.O_RDONLY | _O_BINARY)
    except OSError:
        raise ConfigError("cannot read config file (I/O error; path redacted)")
    try:
        with os.fdopen(fd, "rb") as fh:
            raw_bytes = fh.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigError("cannot read config file (I/O error; path redacted)")
    if len(raw_bytes) > MAX_CONFIG_BYTES:
        raise ConfigError(f"config file too large (max {MAX_CONFIG_BYTES} bytes)")
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("config file is not valid UTF-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}") from exc
    return validate_config(raw)


def write_composition(config: dict[str, Any], out_path: Path, force: bool = False) -> Path:
    """
    Writes the built composition HTML as a brand-new file created atomically
    with O_CREAT|O_EXCL. Refuses unsafe output aliases (symlinks, junctions,
    reparse points) and refuses to overwrite without explicit force; --force
    only ever rewrites a plain regular file. Never removes anything. Filesystem
    paths are never echoed in diagnostics.
    """
    if out_path.suffix.lower() != ".html":
        raise OutputError("output must have .html suffix (path redacted)")
    if _contains_reparse_point(out_path):
        raise OutputError("output path is or lives under a reparse point (symlink/junction); refusing unsafe alias (path redacted)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_html(config)

    fd: int | None = None
    try:
        fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o666)
    except FileExistsError:
        if not force:
            raise OutputError("output already exists (refusing overwrite without --force; nothing was removed; path redacted)")
        try:
            st = os.lstat(out_path)
        except OSError:
            raise OutputError("cannot inspect existing output (I/O error; path redacted)")
        if not stat.S_ISREG(st.st_mode):
            raise OutputError("refusing to overwrite a non-regular output (path redacted)")
        try:
            fd = os.open(str(out_path), os.O_WRONLY | os.O_TRUNC | _O_BINARY)
        except OSError:
            raise OutputError("cannot rewrite existing output (I/O error; path redacted)")
    except OSError:
        raise OutputError("cannot create output file (I/O error; path redacted)")

    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(html.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        raise OutputError("cannot write output file (I/O error; path redacted)")
    return out_path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Build a JSON-configured architecture flow HTML composition for html-video."
    )
    ap.add_argument("--config", required=True, type=Path, help="Path to the flow JSON config.")
    ap.add_argument("--out", required=True, type=Path, help="Path of the new HTML composition to write.")
    ap.add_argument("--force", action="store_true", default=False, help="Allow rewriting an existing plain regular output file (aliases are always refused).")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_and_validate(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        out_path = write_composition(config, args.out, force=args.force)
    except OutputError as exc:
        print(f"output error: {exc}", file=sys.stderr)
        return 3

    try:
        digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
        size_bytes = out_path.stat().st_size
    except OSError:
        print("output error: post-write verification failed (I/O error; path redacted)", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "status": "built",
                "out": str(out_path.resolve()),
                "sha256": digest,
                "size_bytes": size_bytes,
                "nodes": len(config["nodes"]),
                "edges": len(config["edges"]),
                "duration_sec": config["duration_sec"],
                "fps": config["fps"],
                "telemetry_mode": config["telemetry_mode"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
