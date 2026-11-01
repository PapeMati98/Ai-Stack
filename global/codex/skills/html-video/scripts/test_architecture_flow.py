#!/usr/bin/env python3
"""
Automated tests for the html-video architecture flow builder.

Covers strict schema validation (types, unknown keys, finite numbers, bounded
labels/nodes/edges, unique safe IDs, valid edge references, telemetry modes),
safe JSON embedding (closing script sequences, &, <, >, Unicode separators),
hostile-label injection safety, output overwrite refusal, CLI behavior, and
timeline-contract compliance of the built example composition.

Complies with workspace rules:
- Accepts --artifact-root to retain all synthetic test fixtures without cleanup.
- Never invokes rmtree, unlink, or temporary directory deletion.
- Enforces PYTHONDONTWRITEBYTECODE=1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
COMPOSITIONS_DIR = SKILL_ROOT / "compositions"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_architecture_flow import (
    ConfigError,
    OutputError,
    build_html,
    json_for_script,
    load_and_validate,
    validate_config,
    write_composition,
)
from render_video import run_hidden_subprocess

DEFAULT_ARTIFACT_ROOT = SCRIPTS_DIR / '.test-artifacts' / 'architecture-flow'
ARTIFACT_ROOT = DEFAULT_ARTIFACT_ROOT


def set_artifact_root(root: Path) -> None:
    global ARTIFACT_ROOT
    ARTIFACT_ROOT = root.resolve()
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)


_explicit_artifact_root: Path | None = None
_cleaned_argv = [sys.argv[0]] if sys.argv else ["test_architecture_flow.py"]
_skip_next = False
for _i in range(1, len(sys.argv)):
    if _skip_next:
        _skip_next = False
        continue
    _arg = sys.argv[_i]
    if _arg == "--artifact-root":
        if _i + 1 < len(sys.argv):
            _explicit_artifact_root = Path(sys.argv[_i + 1])
            set_artifact_root(_explicit_artifact_root)
            _skip_next = True
        continue
    elif _arg.startswith("--artifact-root="):
        _explicit_artifact_root = Path(_arg.split("=", 1)[1])
        set_artifact_root(_explicit_artifact_root)
        continue
    _cleaned_argv.append(_arg)

sys.argv = _cleaned_argv


def get_fixture_dir(name: str) -> Path:
    target = ARTIFACT_ROOT / "unit_tests" / name
    target.mkdir(parents=True, exist_ok=True)
    return target


def minimal_config() -> dict:
    return {
        "schema_version": 1,
        "title": "Przepływ testowy",
        "grid": {"cols": 2, "rows": 1},
        "nodes": [
            {"id": "a", "label": "Węzeł źródłowy", "kind": "source", "col": 0, "row": 0},
            {"id": "b", "label": "Węzeł docelowy", "kind": "sink", "col": 1, "row": 0},
        ],
        "edges": [
            {"id": "e-a-b", "from": "a", "to": "b", "kind": "data"},
        ],
    }


class TestSchemaValidation(unittest.TestCase):
    """Verifies strict typed schema, known keys, finite numbers, and bounded collections."""

    def test_minimal_config_validates(self) -> None:
        cfg = validate_config(minimal_config())
        self.assertEqual(cfg["telemetry_mode"], "illustrative")
        self.assertEqual(cfg["duration_sec"], 6.0)
        self.assertEqual(cfg["fps"], 30)
        self.assertEqual(cfg["packets_per_edge"], 3)
        self.assertEqual(cfg["counters"]["records_target"], 1000)
        self.assertEqual(len(cfg["nodes"]), 2)
        self.assertEqual(len(cfg["edges"]), 1)

    def test_example_file_validates(self) -> None:
        example = COMPOSITIONS_DIR / "architecture_flow.example.json"
        self.assertTrue(example.is_file(), f"Missing example config: {example}")
        cfg = load_and_validate(example)
        self.assertEqual(cfg["schema_version"], 1)
        self.assertEqual(cfg["telemetry_mode"], "illustrative")
        self.assertGreaterEqual(len(cfg["nodes"]), 2)
        self.assertGreaterEqual(len(cfg["edges"]), 1)

    def test_unknown_top_level_key_rejected(self) -> None:
        cfg = minimal_config()
        cfg["callback_url"] = "https://example.invalid/hook"
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("unknown key", str(cm.exception))

    def test_unknown_node_key_rejected(self) -> None:
        cfg = minimal_config()
        cfg["nodes"][0]["style"] = {"color": "red"}
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("$.nodes[0]", str(cm.exception))
        self.assertIn("unknown key", str(cm.exception))

    def test_unknown_edge_key_rejected(self) -> None:
        cfg = minimal_config()
        cfg["edges"][0]["onclick"] = "alert(1)"
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_unknown_grid_and_counter_keys_rejected(self) -> None:
        cfg = minimal_config()
        cfg["grid"]["origin"] = "top-left"
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg2 = minimal_config()
        cfg2["counters"] = {"secret_path": "C:/Windows"}
        with self.assertRaises(ConfigError):
            validate_config(cfg2)

    def test_wrong_types_rejected(self) -> None:
        cfg = minimal_config()
        cfg["duration_sec"] = "6.0"
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["fps"] = 30.5
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["nodes"] = "not-a-list"
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_bool_rejected_where_number_expected(self) -> None:
        cfg = minimal_config()
        cfg["fps"] = True
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["nodes"][0]["col"] = False
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_non_finite_numbers_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            cfg = minimal_config()
            cfg["duration_sec"] = bad
            with self.assertRaises(ConfigError) as cm:
                validate_config(cfg)
            self.assertIn("finite", str(cm.exception))

    def test_out_of_range_values_rejected(self) -> None:
        cfg = minimal_config()
        cfg["duration_sec"] = 0.1
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["duration_sec"] = 999.0
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["fps"] = 240
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["packets_per_edge"] = 64
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["grid"] = {"cols": 99, "rows": 1}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_node_and_edge_count_bounds(self) -> None:
        cfg = minimal_config()
        cfg["nodes"] = cfg["nodes"][:1]
        cfg["edges"] = []
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg = minimal_config()
        cfg["grid"] = {"cols": 6, "rows": 5}
        cfg["nodes"] = [
            {"id": f"n{i:02d}", "label": f"Wezel {i}", "kind": "process", "col": i % 6, "row": i // 6}
            for i in range(25)
        ]
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("$.nodes", str(cm.exception))

    def test_duplicate_node_ids_rejected(self) -> None:
        cfg = minimal_config()
        cfg["grid"] = {"cols": 3, "rows": 1}
        cfg["nodes"].append({"id": "a", "label": "Duplikat", "kind": "process", "col": 2, "row": 0})
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("duplicate node ID", str(cm.exception))

    def test_duplicate_edge_ids_rejected(self) -> None:
        cfg = minimal_config()
        cfg["edges"].append({"id": "e-a-b", "from": "b", "to": "a"})
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("duplicate edge ID", str(cm.exception))

    def test_colliding_grid_positions_rejected(self) -> None:
        cfg = minimal_config()
        cfg["nodes"][1]["col"] = 0
        cfg["nodes"][1]["row"] = 0
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("collides", str(cm.exception))

    def test_invalid_edge_references_rejected(self) -> None:
        cfg = minimal_config()
        cfg["edges"][0]["to"] = "ghost"
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("unknown node ID", str(cm.exception))

        cfg = minimal_config()
        cfg["edges"][0]["from"] = "a"
        cfg["edges"][0]["to"] = "a"
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("self-loop", str(cm.exception))

    def test_duplicate_ordered_edge_pair_rejected(self) -> None:
        cfg = minimal_config()
        cfg["edges"].append({"id": "e-a-b2", "from": "a", "to": "b"})
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("duplicate edge", str(cm.exception))

    def test_unsafe_ids_rejected(self) -> None:
        for bad_id in ("<script>", "A-upper", "with space", "x" * 40, "-lead", "node/evil", ""):
            cfg = minimal_config()
            cfg["nodes"][0]["id"] = bad_id
            cfg["edges"][0]["from"] = bad_id
            with self.assertRaises(ConfigError, msg=f"Should reject id {bad_id!r}"):
                validate_config(cfg)

    def test_label_bounds_and_control_chars_rejected(self) -> None:
        cfg = minimal_config()
        cfg["nodes"][0]["label"] = "x" * 65
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("too long", str(cm.exception))

        cfg = minimal_config()
        cfg["nodes"][0]["label"] = "zla\netykieta"
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertIn("control character", str(cm.exception))

        cfg = minimal_config()
        cfg["nodes"][0]["label"] = "separator\u2028line"
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg = minimal_config()
        cfg["edges"][0]["label"] = "y" * 49
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg = minimal_config()
        cfg["title"] = "t" * 121
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg = minimal_config()
        cfg["title"] = "   "
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_telemetry_modes_rejected(self) -> None:
        for mode in ("live", "measured", "office", "production", "real", 1, None):
            cfg = minimal_config()
            cfg["telemetry_mode"] = mode
            with self.assertRaises(ConfigError, msg=f"Should reject mode {mode!r}") as cm:
                validate_config(cfg)
            self.assertIn("telemetry_mode", str(cm.exception))

    def test_counters_bounds_and_types(self) -> None:
        cfg = minimal_config()
        cfg["counters"] = {"records_target": -1}
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["counters"] = {"error_budget": 2.5}
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["counters"] = {"queue_max": 10**9}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_log_messages_bounds(self) -> None:
        cfg = minimal_config()
        cfg["log_messages"] = [f"wpis {i}" for i in range(13)]
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["log_messages"] = ["x" * 121]
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = minimal_config()
        cfg["log_messages"] = [42]
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_error_messages_redact_offending_value(self) -> None:
        secretish = "SECRET_TOKEN_ABC_" + "9" * 80
        cfg = minimal_config()
        cfg["nodes"][0]["label"] = secretish
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertNotIn(secretish, str(cm.exception))
        self.assertNotIn("SECRET_TOKEN_ABC", str(cm.exception))

        cfg = minimal_config()
        cfg["nodes"][0]["id"] = "BAD_ID_SECRET_VALUE"
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        self.assertNotIn("BAD_ID_SECRET_VALUE", str(cm.exception))

    def test_unknown_hostile_key_not_echoed(self) -> None:
        sentinel = "SYNTHETIC_SENTINEL_67421"
        cfg = minimal_config()
        cfg["password"] = sentinel
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        msg = str(cm.exception)
        self.assertIn("unknown key", msg)
        self.assertNotIn(sentinel, msg)
        self.assertNotIn("password", msg)

        cfg = minimal_config()
        cfg["nodes"][0]["api_key"] = sentinel
        with self.assertRaises(ConfigError) as cm:
            validate_config(cfg)
        msg = str(cm.exception)
        self.assertIn("$.nodes[0]", msg)
        self.assertNotIn(sentinel, msg)
        self.assertNotIn("api_key", msg)

    def test_missing_config_path_not_echoed(self) -> None:
        missing = ARTIFACT_ROOT / "synthetic_secret_dir_67421" / "missing_config.json"
        with self.assertRaises(ConfigError) as cm:
            load_and_validate(missing)
        msg = str(cm.exception)
        self.assertNotIn("synthetic_secret_dir_67421", msg)
        self.assertNotIn("missing_config", msg)
        self.assertNotIn(str(missing), msg)

    def test_unreadable_config_error_redacted(self) -> None:
        fixture_dir = get_fixture_dir("redaction")
        cfg_path = fixture_dir / "cfg.json"
        cfg_path.write_text('{"schema_version": 1, "telemetry_mode": "live"}', encoding="utf-8")
        with self.assertRaises(ConfigError) as cm:
            load_and_validate(cfg_path)
        self.assertNotIn(str(cfg_path), str(cm.exception))


class TestSafeJsonEmbedding(unittest.TestCase):
    """Verifies script-safe JSON embedding for hostile string content."""

    def test_escapes_closing_script_and_markup_chars(self) -> None:
        cfg = validate_config(minimal_config())
        cfg["title"] = '</script><script>alert("x")</script>&<>'
        blob = json_for_script(cfg)
        self.assertNotIn("</script>", blob)
        self.assertNotIn("<", blob)
        self.assertNotIn(">", blob)
        self.assertNotIn("&", blob)
        self.assertEqual(json.loads(blob), cfg)

    def test_escapes_unicode_separators(self) -> None:
        cfg = validate_config(minimal_config())
        # U+2028/U+2029 are rejected in labels, but the embedding layer must still
        # escape them defensively if they ever appear in any string value.
        cfg["title"] = "line\u2028sep\u2029end"
        blob = json_for_script(cfg)
        self.assertNotIn("\u2028", blob)
        self.assertNotIn("\u2029", blob)
        self.assertEqual(json.loads(blob), cfg)

    def test_roundtrip_preserves_polish_diacritics(self) -> None:
        cfg = validate_config(minimal_config())
        cfg["title"] = "Zażółć gęślą jaźń — łącze ąćęłńóśźż"
        blob = json_for_script(cfg)
        self.assertEqual(json.loads(blob)["title"], cfg["title"])


class TestHostileConfigBuild(unittest.TestCase):
    """Verifies that hostile labels become inert text, never markup or code."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("hostile_build")

    def tearDown(self) -> None:
        pass

    def _hostile_config(self) -> dict:
        cfg = minimal_config()
        cfg["title"] = '</script><script>window.__afPwned=1</script>'
        cfg["subtitle"] = '<img src="https://evil.invalid/x.png" onerror="alert(1)">'
        cfg["badge"] = '&<b>"quoted"</b>'
        cfg["nodes"][0]["label"] = '<svg onload=alert(1)> "cudzysłów" & <tag>'
        cfg["nodes"][1]["label"] = 'javascript:alert(1) https://evil.invalid/payload'
        cfg["edges"][0]["label"] = '</script><iframe src="https://evil.invalid">'
        cfg["log_messages"] = ['<script>alert("log")</script>', 'normalny wpis & <nawiasy>']
        return validate_config(cfg)

    def test_hostile_build_produces_inert_markup(self) -> None:
        cfg = self._hostile_config()
        html = build_html(cfg)

        # Exactly the two template script blocks; the hostile payload is escaped
        # inside the JSON blob, so it cannot add or close any markup.
        self.assertEqual(html.count("<script"), 2)
        self.assertEqual(html.count("</script"), 2)
        self.assertNotIn("<script>window.__afPwned", html)
        self.assertNotIn("<iframe", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<svg onload", html)
        # Attribute-like fragments survive only as inert JSON string data.
        self.assertIn("onerror=", html)

        # Embedded config still round-trips to the original hostile strings.
        start = html.index('type="application/json">') + len('type="application/json">')
        end = html.index("</script>", start)
        embedded = json.loads(html[start:end])
        self.assertEqual(embedded["nodes"][0]["label"], cfg["nodes"][0]["label"])
        self.assertEqual(embedded["edges"][0]["label"], cfg["edges"][0]["label"])

    def test_hostile_write_composition_retained(self) -> None:
        cfg = self._hostile_config()
        out = self.fixture_dir / "hostile_composition.html"
        write_composition(cfg, out, force=True)
        self.assertTrue(out.is_file())
        content = out.read_text(encoding="utf-8")
        self.assertEqual(content.count("<script"), 2)


class TestOutputRefusal(unittest.TestCase):
    """Verifies overwrite refusal and output path constraints."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("output_refusal")

    def tearDown(self) -> None:
        pass

    def test_refuses_overwrite_without_force(self) -> None:
        cfg = validate_config(minimal_config())
        out = self.fixture_dir / "existing.html"
        write_composition(cfg, out, force=True)
        with self.assertRaises(OutputError) as cm:
            write_composition(cfg, out, force=False)
        self.assertIn("refusing overwrite", str(cm.exception))

    def test_force_allows_overwrite(self) -> None:
        cfg = validate_config(minimal_config())
        out = self.fixture_dir / "forced.html"
        write_composition(cfg, out, force=True)
        write_composition(cfg, out, force=True)
        self.assertTrue(out.is_file())

    def test_non_html_suffix_rejected(self) -> None:
        cfg = validate_config(minimal_config())
        with self.assertRaises(OutputError):
            write_composition(cfg, self.fixture_dir / "evil.exe", force=True)

    def test_output_path_not_echoed_in_refusal(self) -> None:
        cfg = validate_config(minimal_config())
        out = self.fixture_dir / "synthetic_secret_out_67421.html"
        write_composition(cfg, out, force=True)
        with self.assertRaises(OutputError) as cm:
            write_composition(cfg, out, force=False)
        msg = str(cm.exception)
        self.assertIn("refusing overwrite", msg)
        self.assertNotIn("synthetic_secret_out_67421", msg)
        self.assertNotIn(str(out), msg)


class TestReparseAndAtomicCreate(unittest.TestCase):
    """Verifies reparse-point refusal and atomic new-file creation semantics."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("reparse_atomic")

    def tearDown(self) -> None:
        pass

    def test_atomic_create_never_truncates_without_force(self) -> None:
        cfg = validate_config(minimal_config())
        out = self.fixture_dir / "atomic.html"
        # force on the initial write keeps the retained fixture re-runnable.
        write_composition(cfg, out, force=True)
        first = out.read_bytes()
        with self.assertRaises(OutputError):
            write_composition(cfg, out, force=False)
        self.assertEqual(out.read_bytes(), first)

    def _ensure_symlink(self, link: Path, target: Path) -> None:
        if link.is_symlink():
            return
        if link.exists():
            self.skipTest(f"retained path is not a symlink: {link.name}")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_output_symlink_refused_even_with_force(self) -> None:
        cfg = validate_config(minimal_config())
        real = self.fixture_dir / "real_target.html"
        write_composition(cfg, real, force=True)
        link = self.fixture_dir / "alias_link.html"
        self._ensure_symlink(link, real)
        before = real.read_bytes()
        with self.assertRaises(OutputError) as cm:
            write_composition(cfg, link, force=True)
        self.assertIn("reparse", str(cm.exception))
        self.assertEqual(real.read_bytes(), before)

    def test_config_symlink_input_rejected(self) -> None:
        cfg_path = self.fixture_dir / "real_config.json"
        cfg_path.write_text(json.dumps(minimal_config()), encoding="utf-8")
        link = self.fixture_dir / "alias_config.json"
        self._ensure_symlink(link, cfg_path)
        with self.assertRaises(ConfigError) as cm:
            load_and_validate(link)
        self.assertIn("reparse", str(cm.exception))

    def test_config_under_junction_rejected(self) -> None:
        target_dir = self.fixture_dir / "junction_target"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "config.json").write_text(json.dumps(minimal_config()), encoding="utf-8")
        junction = self.fixture_dir / "junction_link"
        if not junction.exists():
            res = run_hidden_subprocess(["cmd.exe", "/c", "mklink", "/J", str(junction), str(target_dir)])
            if res.returncode != 0 or not junction.exists():
                self.skipTest("junction creation unavailable")
        try:
            attrs = os.lstat(junction).st_file_attributes
        except (OSError, AttributeError):
            self.skipTest("cannot inspect junction attributes")
        if not (attrs & 0x400):
            self.skipTest("retained path is not a reparse point")
        with self.assertRaises(ConfigError) as cm:
            load_and_validate(junction / "config.json")
        self.assertIn("reparse", str(cm.exception))
        out = junction / "out.html"
        with self.assertRaises(OutputError):
            write_composition(validate_config(minimal_config()), out, force=True)

    def test_oversized_config_rejected(self) -> None:
        cfg_path = self.fixture_dir / "oversized.json"
        cfg_path.write_bytes(b" " * (262_144 + 16))
        with self.assertRaises(ConfigError) as cm:
            load_and_validate(cfg_path)
        self.assertIn("too large", str(cm.exception))


class TestBuilderCLI(unittest.TestCase):
    """Verifies CLI behavior: success, overwrite refusal exit code, config error exit code."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("builder_cli")
        self.builder = SCRIPTS_DIR / "build_architecture_flow.py"
        self.example = COMPOSITIONS_DIR / "architecture_flow.example.json"

    def tearDown(self) -> None:
        pass

    def _run_builder(self, *args: str) -> "tuple[int, str, str]":
        res = run_hidden_subprocess([sys.executable, "-B", str(self.builder), *args])
        return res.returncode, res.stdout, res.stderr

    def test_cli_success_and_overwrite_refusal(self) -> None:
        out = self.fixture_dir / "cli_build.html"
        # --force on the first build keeps the retained fixture re-runnable.
        code, stdout, _ = self._run_builder("--config", str(self.example), "--out", str(out), "--force")
        self.assertEqual(code, 0)
        summary = json.loads(stdout)
        self.assertEqual(summary["status"], "built")
        self.assertEqual(summary["telemetry_mode"], "illustrative")
        self.assertEqual(len(summary["sha256"]), 64)
        self.assertTrue(out.is_file())

        code2, _, stderr2 = self._run_builder("--config", str(self.example), "--out", str(out))
        self.assertEqual(code2, 3)
        self.assertIn("refusing overwrite", stderr2)

        code3, _, _ = self._run_builder("--config", str(self.example), "--out", str(out), "--force")
        self.assertEqual(code3, 0)

    def test_cli_config_error_exit_code(self) -> None:
        bad = self.fixture_dir / "bad_config.json"
        bad.write_text('{"schema_version": 1, "telemetry_mode": "live"}', encoding="utf-8")
        code, _, stderr = self._run_builder("--config", str(bad), "--out", str(self.fixture_dir / "bad_out.html"))
        self.assertEqual(code, 2)
        self.assertIn("config error", stderr)
        self.assertFalse((self.fixture_dir / "bad_out.html").exists())

    def test_cli_invalid_json_exit_code(self) -> None:
        bad = self.fixture_dir / "broken.json"
        bad.write_text("{not json", encoding="utf-8")
        code, _, stderr = self._run_builder("--config", str(bad), "--out", str(self.fixture_dir / "broken_out.html"))
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", stderr)


class TestCompositionCompliance(unittest.TestCase):
    """Verifies the built example composition meets the deterministic timeline contract."""

    BANNED_TOKENS = (
        "innerHTML",
        "outerHTML",
        "document.write",
        "Date.now(",
        "performance.now(",
        "Math.random(",
        "setTimeout(",
        "setInterval(",
        "requestAnimationFrame",
        "fetch(",
        "XMLHttpRequest",
        "new WebSocket",
        "eval(",
        "new Function",
        "<iframe",
        "<img",
        "import(",
        "importScripts",
    )

    def test_built_example_compliance(self) -> None:
        comp = COMPOSITIONS_DIR / "architecture_flow.html"
        self.assertTrue(comp.is_file(), f"Missing built composition: {comp}")
        content = comp.read_text(encoding="utf-8")

        self.assertIn("window.renderTimeline", content)
        self.assertIn("window.seek", content)
        self.assertIn("duration: DURATION", content)
        self.assertIn("fps: FPS", content)
        self.assertIn("aspectRatio: aspect", content)
        self.assertIn("data-timeline-ready", content)
        self.assertIn("document.fonts.ready", content)
        self.assertIn("createElementNS", content)
        self.assertIn("textContent", content)

        for token in self.BANNED_TOKENS:
            self.assertNotIn(token, content, f"Forbidden token in composition: {token}")

        # Illustrative labeling must be visible so counters cannot impersonate live metrics.
        self.assertIn("ILUSTRACYJNE", content)
        self.assertIn("nie telemetria produkcyjna", content)

        # The last sampled frame (t=duration-1/fps) must present the completed
        # flow; the end-snap keeps the pure timeline mapping while guaranteeing
        # the rendered MP4 contains the finished log/counter state.
        self.assertIn("endSnapT", content)
        self.assertIn("DURATION - 1 / FPS", content)

    def test_built_example_matches_example_config(self) -> None:
        comp = COMPOSITIONS_DIR / "architecture_flow.html"
        cfg = load_and_validate(COMPOSITIONS_DIR / "architecture_flow.example.json")
        content = comp.read_text(encoding="utf-8")
        start = content.index('type="application/json">') + len('type="application/json">')
        end = content.index("</script>", start)
        embedded = json.loads(content[start:end])
        self.assertEqual(embedded, cfg)

    def test_template_has_no_external_resources(self) -> None:
        comp = COMPOSITIONS_DIR / "architecture_flow.html"
        content = comp.read_text(encoding="utf-8")
        # Only the inert SVG XML namespace identifier may contain an http URI.
        without_ns = content.replace("http://www.w3.org/2000/svg", "")
        self.assertNotIn("http://", without_ns)
        self.assertNotIn("https://", without_ns)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test suite for the html-video architecture flow builder.")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="Root directory for retained synthetic test fixtures and evidence.",
    )
    args, _ = parser.parse_known_args()
    target_root = _explicit_artifact_root or args.artifact_root
    set_artifact_root(target_root)

    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
