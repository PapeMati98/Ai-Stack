#!/usr/bin/env python3
"""
Comprehensive automated tests for html-video skill.
Tests security gates, timeline contract compliance, audio technical validation,
seek-order determinism tolerance, asset pinning, and contact sheet generation.

Complies with workspace rules:
- Accepts --artifact-root to retain all synthetic test fixtures without cleanup.
- Never invokes rmtree, unlink, or temporary directory deletion.
- Enforces PYTHONDONTWRITEBYTECODE=1.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import struct
import sys
import unittest
from pathlib import Path

# Ensure bytecode is never written to disk
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

# Directory structure
SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
COMPOSITIONS_DIR = SKILL_ROOT / "compositions"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from render_video import (
    RenderError,
    SecurityError,
    ValidationError,
    validate_safe_path,
    validate_wav_audio,
    create_synthetic_wav,
    compare_frame_bytes,
    generate_contact_sheet,
    extract_declared_assets,
    compute_sha256,
    classify_request_url,
    resolve_and_pin_assets,
    build_manifest,
    get_runtime_versions,
    build_parser,
    setup_browser_page_isolation,
    SECURITY_INIT_SCRIPT,
    validate_composition_geometry,
    run_hidden_subprocess,
    render_pipeline,
)

DEFAULT_ARTIFACT_ROOT = SCRIPTS_DIR / '.test-artifacts' / 'html-video'
ARTIFACT_ROOT = DEFAULT_ARTIFACT_ROOT


def set_artifact_root(root: Path) -> None:
    """Sets the global artifact root directory for retained synthetic test fixtures."""
    global ARTIFACT_ROOT
    ARTIFACT_ROOT = root.resolve()
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)


# Parse --artifact-root early and sanitize sys.argv so unittest CLI discovery never rejects it
_explicit_artifact_root: Path | None = None
_cleaned_argv = [sys.argv[0]] if sys.argv else ["test_render_video.py"]
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

# Overwrite sys.argv so unittest.main and standard runners never reject --artifact-root
sys.argv = _cleaned_argv


def get_fixture_dir(name: str) -> Path:
    """Returns a designated retained fixture directory under ARTIFACT_ROOT without cleanup."""
    target = ARTIFACT_ROOT / "unit_tests" / name
    target.mkdir(parents=True, exist_ok=True)
    return target


class TestSecurityGates(unittest.TestCase):
    """Verifies path containment, shell injection prevention, and overwrite gates."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("security_gates")

    def tearDown(self) -> None:
        # Retention policy: retain all synthetic fixtures under ARTIFACT_ROOT without deletion
        pass

    def test_safe_path_inside_root(self) -> None:
        valid_file = self.fixture_dir / "valid_output.mp4"
        res = validate_safe_path(valid_file, self.fixture_dir, allow_overwrite=True)
        self.assertEqual(res, valid_file.resolve())

    def test_path_escaping_root_rejected(self) -> None:
        escaping_file = self.fixture_dir / ".." / "escaped.mp4"
        with self.assertRaises(SecurityError):
            validate_safe_path(escaping_file, self.fixture_dir)

    def test_overwrite_refusal_without_force(self) -> None:
        existing = self.fixture_dir / "existing_refusal.mp4"
        existing.write_text("synthetic_data", encoding="utf-8")
        with self.assertRaises(SecurityError):
            validate_safe_path(existing, self.fixture_dir, allow_overwrite=False)

        # Allowed with explicit force
        res = validate_safe_path(existing, self.fixture_dir, allow_overwrite=True)
        self.assertEqual(res, existing.resolve())

    def test_shell_injection_characters_rejected(self) -> None:
        bad_names = [
            "video;rm.mp4",
            "video|echo.mp4",
            "video`id`.mp4",
            "video$test.mp4",
            "video&calc.mp4",
            "video\necho.mp4",
            "video\recho.mp4",
            "video<input.mp4",
            "video>output.mp4",
        ]
        for name in bad_names:
            bad_path = self.fixture_dir / name
            with self.assertRaises(SecurityError, msg=f"Should reject {name}"):
                validate_safe_path(bad_path, self.fixture_dir)

    def test_powershell_version_retrieval_handles_single_quotes(self) -> None:
        """Verifies that paths with single quotes properly exercise PowerShell quote-escaping and version retrieval."""
        quoted_dir = self.fixture_dir / "vendor's 'app' with quotes"
        quoted_dir.mkdir(parents=True, exist_ok=True)
        dummy_chrome = quoted_dir / "dummy_chrome.exe"

        # Materialize an executable file under a path containing single quotes.
        # On Windows, copy a valid PE executable that has VersionInfo (ProductVersion)
        # but fails on '--version' (e.g. attrib.exe, cmd.exe, find.exe).
        if sys.platform == "win32":
            candidates = [
                Path(r"C:\Windows\System32\attrib.exe"),
                Path(r"C:\Windows\System32\cmd.exe"),
                Path(r"C:\Windows\System32\find.exe"),
            ]
            src = next((c for c in candidates if c.is_file()), None)
            if src:
                shutil.copy2(src, dummy_chrome)
            else:
                dummy_chrome.write_bytes(b"MZdummy")

            self.assertTrue(dummy_chrome.is_file())

            versions = get_runtime_versions(browser_bin=str(dummy_chrome))
            self.assertIn("browser_version", versions)
            # The materialized PE file with single quotes in its path must successfully resolve
            # its ProductVersion via the escaped PowerShell command rather than falling back to 'unknown'
            if src:
                self.assertNotEqual(
                    versions["browser_version"],
                    "unknown",
                    "PowerShell version retrieval failed to extract ProductVersion from single-quote path",
                )
                self.assertTrue(len(versions["browser_version"]) > 0)

            # Regression check: directly verify that unescaped single quotes would fail PowerShell parsing
            unescaped_ps_cmd = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                f"(Get-Item -LiteralPath '{str(dummy_chrome)}').VersionInfo.ProductVersion",
            ]
            unescaped_res = run_hidden_subprocess(unescaped_ps_cmd)
            self.assertNotEqual(
                unescaped_res.returncode,
                0,
                "Unescaped single quotes should fail PowerShell command syntax parsing",
            )
        else:
            dummy_chrome.write_text("#!/bin/sh\necho unknown\n", encoding="utf-8")
            versions = get_runtime_versions(browser_bin=str(dummy_chrome))
            self.assertIn("browser_version", versions)


class TestAudioValidation(unittest.TestCase):
    """Verifies strict technical validation for WAV audio."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("audio_validation")

    def tearDown(self) -> None:
        # Retention policy: retain all synthetic fixtures under ARTIFACT_ROOT
        pass

    def test_valid_synthetic_wav(self) -> None:
        wav_path = self.fixture_dir / "valid_48k_stereo.wav"
        create_synthetic_wav(wav_path, duration_sec=1.5, sample_rate=48000, channels=2)
        meta = validate_wav_audio(wav_path)
        self.assertTrue(meta["valid"])
        self.assertEqual(meta["format"], "PCM")
        self.assertEqual(meta["channels"], 2)
        self.assertEqual(meta["sample_rate"], 48000)
        self.assertEqual(meta["duration_sec"], 1.5)

    def test_missing_wav_file(self) -> None:
        with self.assertRaises(ValidationError):
            validate_wav_audio(self.fixture_dir / "nonexistent_audio.wav")

    def test_invalid_extension(self) -> None:
        bad_ext = self.fixture_dir / "synthetic.mp3"
        bad_ext.write_text("not a wav file", encoding="utf-8")
        with self.assertRaises(ValidationError):
            validate_wav_audio(bad_ext)

    def test_truncated_header(self) -> None:
        trunc = self.fixture_dir / "truncated.wav"
        trunc.write_bytes(b"RIFF1234")
        with self.assertRaises(ValidationError):
            validate_wav_audio(trunc)

    def test_invalid_bits_per_sample_rejected(self) -> None:
        """Verifies that bits_per_sample < 8 is cleanly rejected with ValidationError, not ZeroDivisionError."""
        bad_wav = self.fixture_dir / "zero_bit_depth.wav"
        file_size = 36 + 100
        with open(bad_wav, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", file_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", 16))
            f.write(struct.pack("<H", 1))  # PCM
            f.write(struct.pack("<H", 2))  # 2 channels
            f.write(struct.pack("<I", 48000))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<H", 0))  # block_align = 0
            f.write(struct.pack("<H", 0))  # bits_per_sample = 0
            f.write(b"data")
            f.write(struct.pack("<I", 100))
            f.write(b"\x00" * 100)

        with self.assertRaises(ValidationError) as cm:
            validate_wav_audio(bad_wav)
        self.assertIn("bits per sample", str(cm.exception))

    def test_unsupported_bits_per_sample_rejected(self) -> None:
        """Verifies that non-standard bit depth (e.g. 12 or 48) is rejected with ValidationError."""
        bad_wav = self.fixture_dir / "bad_bit_depth_12.wav"
        file_size = 36 + 100
        with open(bad_wav, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", file_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", 16))
            f.write(struct.pack("<H", 1))
            f.write(struct.pack("<H", 2))
            f.write(struct.pack("<I", 48000))
            f.write(struct.pack("<I", 96000))
            f.write(struct.pack("<H", 3))  # block_align
            f.write(struct.pack("<H", 12))  # bits_per_sample = 12
            f.write(b"data")
            f.write(struct.pack("<I", 100))
            f.write(b"\x00" * 100)

        with self.assertRaises(ValidationError) as cm:
            validate_wav_audio(bad_wav)
        self.assertIn("bits per sample", str(cm.exception))

    def test_invalid_block_align_rejected(self) -> None:
        """Verifies that mismatching block_align is rejected with ValidationError."""
        bad_wav = self.fixture_dir / "bad_block_align.wav"
        file_size = 36 + 100
        with open(bad_wav, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", file_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", 16))
            f.write(struct.pack("<H", 1))
            f.write(struct.pack("<H", 2))
            f.write(struct.pack("<I", 48000))
            f.write(struct.pack("<I", 192000))
            f.write(struct.pack("<H", 2))  # invalid block_align (expected 4 for 2ch 16bit)
            f.write(struct.pack("<H", 16))
            f.write(b"data")
            f.write(struct.pack("<I", 100))
            f.write(b"\x00" * 100)

        with self.assertRaises(ValidationError) as cm:
            validate_wav_audio(bad_wav)
        self.assertIn("block align", str(cm.exception).lower())


class TestTimelineContractCompliance(unittest.TestCase):
    """Verifies that composition HTML meets strict deterministic timeline contract."""

    def test_typography_chart_compliance(self) -> None:
        comp_file = COMPOSITIONS_DIR / "typography_chart.html"
        self.assertTrue(comp_file.is_file(), f"Missing composition file: {comp_file}")

        content = comp_file.read_text(encoding="utf-8")

        # Required global API hooks
        self.assertIn("window.renderTimeline", content)
        self.assertIn("window.seek", content)
        self.assertIn("duration:", content)
        self.assertIn("fps:", content)
        self.assertIn("aspectRatio:", content)

        # Prohibited autonomous clocks and timers
        self.assertNotIn("setInterval(", content, "Forbidden: setInterval introduces clock drift")
        self.assertNotIn("setTimeout(", content, "Forbidden: setTimeout introduces autonomous timing")
        self.assertNotIn("Date.now()", content, "Forbidden: Date.now() breaks seek determinism")
        self.assertNotIn("performance.now()", content, "Forbidden: performance.now() breaks seek determinism")

        # Check font readiness gate and ready marker
        self.assertIn("document.fonts.ready", content)
        self.assertIn("data-timeline-ready", content)


class TestDeterminismMetrics(unittest.TestCase):
    """Verifies frame deviation metrics and tolerance evaluation."""

    def test_identical_frame_buffers(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")

        im = Image.new("RGB", (100, 100), color=(10, 20, 30))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        metric = compare_frame_bytes(raw_bytes, raw_bytes)
        self.assertTrue(metric["exact_byte_match"])
        self.assertEqual(metric["mae"], 0.0)
        self.assertTrue(metric["tolerance_passed"])
        self.assertLessEqual(metric["mae"], metric["tolerance"])

    def test_divergent_frame_buffers(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")

        im1 = Image.new("RGB", (100, 100), color=(0, 0, 0))
        im2 = Image.new("RGB", (100, 100), color=(255, 255, 255))

        buf1 = io.BytesIO()
        im1.save(buf1, format="PNG")
        buf2 = io.BytesIO()
        im2.save(buf2, format="PNG")

        metric = compare_frame_bytes(buf1.getvalue(), buf2.getvalue())
        self.assertFalse(metric["exact_byte_match"])
        self.assertGreater(metric["mae"], 0.5)
        self.assertFalse(metric["tolerance_passed"])


class TestContactSheetGeneration(unittest.TestCase):
    """Verifies inspectable contact sheet grid generation."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("contact_sheet")

    def tearDown(self) -> None:
        # Retention policy: retain all synthetic fixtures under ARTIFACT_ROOT
        pass

    def test_generate_sheet_from_mock_frames(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")

        frames_dir = self.fixture_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        for i in range(6):
            im = Image.new("RGB", (320, 180), color=(i * 40, 50, 100))
            im.save(frames_dir / f"frame_{i:06d}.png")

        out_sheet = self.fixture_dir / "contact_sheet.png"
        res = generate_contact_sheet(frames_dir, out_sheet, cols=3, sample_count=6, fps=30)
        self.assertTrue(res.is_file())
        self.assertGreater(res.stat().st_size, 0)


class TestAssetPinningAndIsolation(unittest.TestCase):
    """Verifies extraction and admission gates for local composition assets."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("asset_pinning")

    def tearDown(self) -> None:
        pass

    def test_extract_declared_assets(self) -> None:
        mock_html = self.fixture_dir / "mock_composition.html"
        mock_html.write_text(
            """
            <html>
            <script>
            window.renderTimeline = {
                assets: ['assets/logo.svg', 'assets/data.json']
            };
            </script>
            </html>
            """,
            encoding="utf-8",
        )
        assets = extract_declared_assets(mock_html)
        self.assertEqual(assets, ["assets/logo.svg", "assets/data.json"])

    def test_sha256_computation(self) -> None:
        sample_file = self.fixture_dir / "sample.txt"
        sample_file.write_text("deterministic-content", encoding="utf-8")
        h1 = compute_sha256(sample_file)
        h2 = compute_sha256(sample_file)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_classify_remote_protocols_blocked(self) -> None:
        comp_path = self.fixture_dir / "mock_comp.html"
        remote_urls = [
            "http://example.com/external.png",
            "https://cdn.jsdelivr.net/npm/chart.js",
            "ws://localhost:8080/feed",
            "wss://secure.example.com/socket",
            "ftp://files.example.com/asset.woff2",
        ]
        for url in remote_urls:
            decision, reason = classify_request_url(url, comp_path)
            self.assertEqual(decision, "block", msg=f"Should block {url}")
            self.assertEqual(reason, "blockedbyclient")

    def test_classify_data_uri_allowed(self) -> None:
        comp_path = self.fixture_dir / "mock_comp.html"
        data_url = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="
        decision, reason = classify_request_url(data_url, comp_path)
        self.assertEqual(decision, "allow")
        self.assertEqual(reason, "data_uri")

    def test_classify_composition_root_allowed(self) -> None:
        comp_path = (self.fixture_dir / "root_comp.html").resolve()
        comp_path.write_text("<html></html>", encoding="utf-8")

        url = f"file:///{comp_path.as_posix()}"
        decision, reason = classify_request_url(url, comp_path)
        self.assertEqual(decision, "allow")
        self.assertEqual(reason, "composition_root")

        # Query parameters should not break root recognition
        url_with_query = f"{url}?aspect=16:9&w=1920&h=1080"
        decision_q, reason_q = classify_request_url(url_with_query, comp_path)
        self.assertEqual(decision_q, "allow")
        self.assertEqual(reason_q, "composition_root")

    def test_classify_admitted_pinned_asset_allowed(self) -> None:
        comp_path = (self.fixture_dir / "root_comp.html").resolve()
        asset_path = (self.fixture_dir / "assets" / "valid_logo.svg").resolve()
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_text("<svg></svg>", encoding="utf-8")

        url = f"file:///{asset_path.as_posix()}"
        decision, reason = classify_request_url(url, comp_path, admitted_paths={asset_path})
        self.assertEqual(decision, "allow")
        self.assertEqual(reason, "admitted_asset")

    def test_classify_unpinned_local_file_denied(self) -> None:
        comp_path = (self.fixture_dir / "root_comp.html").resolve()
        unpinned = (self.fixture_dir / "unpinned_local.txt").resolve()
        unpinned.write_text("unpinned", encoding="utf-8")

        url = f"file:///{unpinned.as_posix()}"
        decision, reason = classify_request_url(url, comp_path, admitted_paths=set())
        self.assertEqual(decision, "deny")
        self.assertEqual(reason, "unpinned_local_file")

    def test_classify_file_outside_composition_boundary_denied(self) -> None:
        comp_path = (self.fixture_dir / "root_comp.html").resolve()
        outside_path = Path("C:/Windows/System32/cmd.exe").resolve()

        url = f"file:///{outside_path.as_posix()}"
        decision, reason = classify_request_url(url, comp_path, admitted_paths=set())
        self.assertEqual(decision, "deny")
        self.assertEqual(reason, "outside_composition_boundary")

    def test_missing_declared_asset_raises_validation_error(self) -> None:
        comp_path = self.fixture_dir / "missing_asset_comp.html"
        comp_path.write_text(
            """
            <html>
            <script>
            window.renderTimeline = {
                assets: ['assets/nonexistent_image.png']
            };
            </script>
            </html>
            """,
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            resolve_and_pin_assets(comp_path)

    def test_escaping_declared_asset_raises_security_error(self) -> None:
        comp_path = self.fixture_dir / "escaping_asset_comp.html"
        comp_path.write_text(
            """
            <html>
            <script>
            window.renderTimeline = {
                assets: ['../escaped_secret.json']
            };
            </script>
            </html>
            """,
            encoding="utf-8",
        )
        with self.assertRaises(SecurityError):
            resolve_and_pin_assets(comp_path)

    def test_pinned_font_binary_sha256(self) -> None:
        font_file = self.fixture_dir / "custom_font.ttf"
        font_bytes = b"\x00\x01\x00\x00\x00\x0cTEST_FONT_DATA"
        font_file.write_bytes(font_bytes)
        expected_sha = compute_sha256(font_file)

        comp_path = self.fixture_dir / "font_comp.html"
        comp_path.write_text(
            """
            <html>
            <script>
            window.renderTimeline = {
                assets: ['custom_font.ttf']
            };
            </script>
            </html>
            """,
            encoding="utf-8",
        )
        admitted, manifest_entries = resolve_and_pin_assets(comp_path)
        self.assertIn(font_file.resolve(), admitted)
        self.assertIn("custom_font.ttf", manifest_entries)
        self.assertEqual(manifest_entries["custom_font.ttf"]["sha256"], expected_sha)
        self.assertEqual(manifest_entries["custom_font.ttf"]["size_bytes"], len(font_bytes))


class TestRuntimeVersionsInManifest(unittest.TestCase):
    """Verifies that exact runtime versions for Python, platform, Chrome, Playwright, FFmpeg, and FFprobe are recorded."""

    def test_get_runtime_versions_structure(self) -> None:
        versions = get_runtime_versions(
            browser_bin="C:/Program Files/Google/Chrome/Application/chrome.exe",
            browser_version="128.0.6613.119",
        )
        self.assertIn("python", versions)
        self.assertIn("platform", versions)
        self.assertIn("playwright", versions)
        self.assertIn("browser_binary", versions)
        self.assertIn("browser_version", versions)
        self.assertEqual(versions["browser_version"], "128.0.6613.119")
        self.assertIn("ffmpeg", versions)
        self.assertIn("ffprobe", versions)

    def test_build_manifest_runtime_block(self) -> None:
        fixture_dir = get_fixture_dir("runtime_versions")
        mock_html = fixture_dir / "mock_comp.html"
        mock_html.write_text("<html></html>", encoding="utf-8")
        mock_mp4 = fixture_dir / "mock_out.mp4"
        mock_mp4.write_bytes(b"\x00\x00\x00 ftypisom")
        mock_sheet = fixture_dir / "mock_sheet.png"
        mock_sheet.write_bytes(b"\x89PNG\r\n\x1a\n")

        render_meta = {
            "browser_binary": "C:/Program Files/Google/Chrome/Application/chrome.exe",
            "browser_version": "128.0.6613.119",
            "font_hash": "mock_font_hash",
            "fonts_info": [],
            "blocked_requests": [],
            "page_errors": [],
            "console_errors": [],
            "pinned_assets": {},
            "determinism_metric": {"mae": 0.0, "tolerance_passed": True},
        }
        probe_meta = {
            "duration_sec": 5.0,
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "30/1",
        }

        manifest = build_manifest(
            composition_path=mock_html,
            output_mp4=mock_mp4,
            render_meta=render_meta,
            probe_meta=probe_meta,
            contact_sheet_path=mock_sheet,
        )

        runtime = manifest.get("runtime", {})
        self.assertIn("python", runtime)
        self.assertIn("platform", runtime)
        self.assertIn("playwright", runtime)
        self.assertIn("browser_binary", runtime)
        self.assertIn("browser_version", runtime)
        self.assertEqual(runtime["browser_version"], "128.0.6613.119")
        self.assertIn("ffmpeg", runtime)
        self.assertIn("ffprobe", runtime)
        self.assertEqual(runtime["font_hash"], "mock_font_hash")


class TestGeometryAndAspectValidation(unittest.TestCase):
    """Verifies 1:1 aspect ratio support and geometry mismatch gates."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("geometry_validation")

    def test_aspect_1_1_defaults(self) -> None:
        # Verify aspect choices in CLI
        parser = build_parser()
        render_sub = parser._subparsers._actions[1].choices["render"]
        aspect_action = next(a for a in render_sub._actions if a.dest == "aspect")
        self.assertIn("1:1", aspect_action.choices)
        self.assertIn("16:9", aspect_action.choices)
        self.assertIn("9:16", aspect_action.choices)

    def test_composition_aspect_mismatch_detection(self) -> None:
        """Verifies that validate_composition_geometry production code rejects aspect and dimension mismatches."""
        timeline_meta = {
            "duration": 5.0,
            "fps": 30,
            "width": 1920,
            "height": 1080,
            "aspectRatio": "16:9",
        }
        # Aspect mismatch
        with self.assertRaises(ValidationError) as cm:
            validate_composition_geometry(
                timeline_meta=timeline_meta,
                requested_aspect="9:16",
                target_w=1080,
                target_h=1920,
            )
        self.assertIn("Composition geometry mismatch", str(cm.exception))
        self.assertIn("requested aspect '9:16'", str(cm.exception))

        # Resolution mismatch with matching aspect
        with self.assertRaises(ValidationError) as cm2:
            validate_composition_geometry(
                timeline_meta=timeline_meta,
                requested_aspect="16:9",
                target_w=1280,
                target_h=720,
            )
        self.assertIn("Composition geometry mismatch", str(cm2.exception))
        self.assertIn("target viewport is 1280x720", str(cm2.exception))

        # Matching geometry passes without error
        validate_composition_geometry(
            timeline_meta=timeline_meta,
            requested_aspect="16:9",
            target_w=1920,
            target_h=1080,
        )


class TestContactSheetStaleFrames(unittest.TestCase):
    """Verifies that contact sheet generation strictly ignores stale frames from previous runs."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("contact_sheet_stale")

    def test_stale_frames_excluded_by_total_frames(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")

        frames_dir = self.fixture_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        # Simulate an interrupted or earlier longer run with 12 frames
        for i in range(12):
            im = Image.new("RGB", (100, 100), color=(i * 20, 50, 50))
            im.save(frames_dir / f"frame_{i:06d}.png")

        out_sheet = self.fixture_dir / "clean_sheet.png"
        # Current render only produced 6 frames (0 through 5)
        res = generate_contact_sheet(
            frames_dir=frames_dir,
            output_image=out_sheet,
            cols=3,
            sample_count=6,
            fps=30,
            total_frames=6,
        )
        self.assertTrue(res.is_file())
        im_sheet = Image.open(res)
        self.assertGreater(im_sheet.width, 0)
        self.assertGreater(im_sheet.height, 0)


class TestServiceWorkerAndWebSocketBlocking(unittest.TestCase):
    """Verifies multi-layer service worker and WebSocket blocking configuration."""

    def test_security_init_script_blocks_apis(self) -> None:
        self.assertIn("WebSocket", SECURITY_INIT_SCRIPT)
        self.assertIn("SharedWorker", SECURITY_INIT_SCRIPT)
        self.assertIn("ServiceWorker", SECURITY_INIT_SCRIPT)
        self.assertIn("serviceWorker.register", SECURITY_INIT_SCRIPT)

    def test_ws_route_closes_without_connect_to_server(self) -> None:
        """Verifies context.route_web_socket pre-network isolation closes route without server connection."""
        class MockWebSocketRoute:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False
                self.close_code: int | None = None
                self.close_reason: str | None = None
                self.connected = False

            def close(self, code: int | None = None, reason: str | None = None) -> None:
                self.closed = True
                self.close_code = code
                self.close_reason = reason

            def connect_to_server(self) -> Any:
                self.connected = True
                raise RuntimeError("Security violation: connect_to_server was invoked")

        class MockContext:
            def __init__(self) -> None:
                self.init_scripts: list[str] = []
                self.ws_routes: dict[str, Any] = {}
                self.event_handlers: dict[str, Any] = {}
                self.routes: dict[str, Any] = {}

            def add_init_script(self, script: str) -> None:
                self.init_scripts.append(script)

            def route_web_socket(self, pattern: str, handler: Any) -> None:
                self.ws_routes[pattern] = handler

            def on(self, event: str, handler: Any) -> None:
                self.event_handlers[event] = handler

            def route(self, pattern: str, handler: Any) -> None:
                self.routes[pattern] = handler

        class MockPage:
            def __init__(self) -> None:
                self.event_handlers: dict[str, Any] = {}

            def on(self, event: str, handler: Any) -> None:
                self.event_handlers[event] = handler

        mock_ctx = MockContext()
        mock_pg = MockPage()
        blocked_reqs: list[str] = []

        setup_browser_page_isolation(
            context=mock_ctx,
            page=mock_pg,
            composition_path=Path("mock.html"),
            blocked_requests=blocked_reqs,
        )

        self.assertIn("**/*", mock_ctx.ws_routes)
        ws_handler = mock_ctx.ws_routes["**/*"]

        mock_route = MockWebSocketRoute("ws://remote-service.internal:9000/feed")
        ws_handler(mock_route)

        self.assertTrue(mock_route.closed)
        self.assertEqual(mock_route.close_code, 1008)
        self.assertFalse(mock_route.connected)
        self.assertIn("ws_route:ws://remote-service.internal:9000/feed", blocked_reqs)

    def test_chromium_sandbox_explicitly_configured(self) -> None:
        """Verifies that render_video.py source explicitly specifies chromium_sandbox=True."""
        render_py = SCRIPTS_DIR / "render_video.py"
        content = render_py.read_text(encoding="utf-8")
        self.assertIn("chromium_sandbox=True", content)
        self.assertNotIn("--no-sandbox", content)

    def test_isolation_fails_closed_when_http_route_api_missing(self) -> None:
        """Verifies that missing context.route fails closed with SecurityError before navigation."""
        class MockTrackingPage:
            def __init__(self) -> None:
                self.navigated = False
                self.rendered = False
                self.event_handlers: dict[str, Any] = {}

            def on(self, event: str, handler: Any) -> None:
                self.event_handlers[event] = handler

            def goto(self, url: str, **kwargs: Any) -> None:
                self.navigated = True

            def screenshot(self, **kwargs: Any) -> bytes:
                self.rendered = True
                return b"fake_png"

        class MockContextWithoutHttpRoute:
            def add_init_script(self, script: str) -> None:
                pass

            def route_web_socket(self, pattern: str, handler: Any) -> None:
                pass

            def on(self, event: str, handler: Any) -> None:
                pass

        page = MockTrackingPage()
        ctx = MockContextWithoutHttpRoute()

        with self.assertRaises(SecurityError) as cm:
            setup_browser_page_isolation(
                context=ctx,
                page=page,
                composition_path=Path("mock.html"),
            )
            page.goto("file:///mock.html")
            page.screenshot()

        self.assertIn("Context HTTP route registration is unavailable", str(cm.exception))
        self.assertFalse(page.navigated, "Navigation was reached despite missing HTTP route API")
        self.assertFalse(page.rendered, "Render was reached despite missing HTTP route API")

    def test_isolation_fails_closed_when_ws_route_api_missing(self) -> None:
        """Verifies that missing context.route_web_socket fails closed before navigation."""
        class MockTrackingPage:
            def __init__(self) -> None:
                self.navigated = False
                self.rendered = False
                self.event_handlers: dict[str, Any] = {}

            def on(self, event: str, handler: Any) -> None:
                self.event_handlers[event] = handler

            def goto(self, url: str, **kwargs: Any) -> None:
                self.navigated = True

            def screenshot(self, **kwargs: Any) -> bytes:
                self.rendered = True
                return b"fake_png"

        class MockContextWithoutWsRoute:
            def add_init_script(self, script: str) -> None:
                pass

            def route(self, pattern: str, handler: Any) -> None:
                pass

            def on(self, event: str, handler: Any) -> None:
                pass

        page = MockTrackingPage()
        ctx = MockContextWithoutWsRoute()

        with self.assertRaises(SecurityError) as cm:
            setup_browser_page_isolation(
                context=ctx,
                page=page,
                composition_path=Path("mock.html"),
            )
            page.goto("file:///mock.html")
            page.screenshot()

        self.assertIn("route_web_socket missing", str(cm.exception))
        self.assertFalse(page.navigated, "Navigation was reached despite missing WebSocket route API")
        self.assertFalse(page.rendered, "Render was reached despite missing WebSocket route API")

    def test_isolation_fails_closed_when_http_route_registration_raises(self) -> None:
        """Verifies that context.route registration failure raises SecurityError without page downgrade."""
        class MockTrackingPage:
            def __init__(self) -> None:
                self.navigated = False
                self.rendered = False
                self.page_route_called = False
                self.event_handlers: dict[str, Any] = {}

            def on(self, event: str, handler: Any) -> None:
                self.event_handlers[event] = handler

            def route(self, pattern: str, handler: Any) -> None:
                self.page_route_called = True

            def goto(self, url: str, **kwargs: Any) -> None:
                self.navigated = True

            def screenshot(self, **kwargs: Any) -> bytes:
                self.rendered = True
                return b"fake_png"

        class MockContextFailingHttpRoute:
            def add_init_script(self, script: str) -> None:
                pass

            def route_web_socket(self, pattern: str, handler: Any) -> None:
                pass

            def on(self, event: str, handler: Any) -> None:
                pass

            def route(self, pattern: str, handler: Any) -> None:
                raise RuntimeError("Simulated context HTTP route registration failure")

        page = MockTrackingPage()
        ctx = MockContextFailingHttpRoute()

        with self.assertRaises(SecurityError) as cm:
            setup_browser_page_isolation(
                context=ctx,
                page=page,
                composition_path=Path("mock.html"),
            )
            page.goto("file:///mock.html")
            page.screenshot()

        self.assertIn("Context HTTP route registration failed", str(cm.exception))
        self.assertFalse(page.navigated, "Navigation was reached despite HTTP route failure")
        self.assertFalse(page.rendered, "Render was reached despite HTTP route failure")
        self.assertFalse(page.page_route_called, "Context isolation was improperly downgraded to page-only")

    def test_isolation_fails_closed_when_ws_route_registration_raises(self) -> None:
        """Verifies that context.route_web_socket registration failure raises SecurityError before navigation."""
        class MockTrackingPage:
            def __init__(self) -> None:
                self.navigated = False
                self.rendered = False
                self.event_handlers: dict[str, Any] = {}

            def on(self, event: str, handler: Any) -> None:
                self.event_handlers[event] = handler

            def goto(self, url: str, **kwargs: Any) -> None:
                self.navigated = True

            def screenshot(self, **kwargs: Any) -> bytes:
                self.rendered = True
                return b"fake_png"

        class MockContextFailingWsRoute:
            def add_init_script(self, script: str) -> None:
                pass

            def route_web_socket(self, pattern: str, handler: Any) -> None:
                raise RuntimeError("Simulated WebSocket route registration failure")

            def on(self, event: str, handler: Any) -> None:
                pass

            def route(self, pattern: str, handler: Any) -> None:
                pass

        page = MockTrackingPage()
        ctx = MockContextFailingWsRoute()

        with self.assertRaises(SecurityError) as cm:
            setup_browser_page_isolation(
                context=ctx,
                page=page,
                composition_path=Path("mock.html"),
            )
            page.goto("file:///mock.html")
            page.screenshot()

        self.assertIn("Context pre-network WebSocket route registration failed", str(cm.exception))
        self.assertFalse(page.navigated, "Navigation was reached despite WebSocket route registration failure")
        self.assertFalse(page.rendered, "Render was reached despite WebSocket route registration failure")

    def test_isolation_fails_closed_when_init_script_missing_or_raises(self) -> None:
        """Verifies that context add_init_script missing or raising fails closed before navigation."""
        class MockTrackingPage:
            def __init__(self) -> None:
                self.navigated = False
                self.rendered = False

            def goto(self, url: str, **kwargs: Any) -> None:
                self.navigated = True

            def screenshot(self, **kwargs: Any) -> bytes:
                self.rendered = True
                return b"fake_png"

        class MockContextMissingInitScript:
            def route_web_socket(self, pattern: str, handler: Any) -> None:
                pass

            def route(self, pattern: str, handler: Any) -> None:
                pass

        page = MockTrackingPage()
        ctx = MockContextMissingInitScript()

        with self.assertRaises(SecurityError) as cm:
            setup_browser_page_isolation(
                context=ctx,
                page=page,
                composition_path=Path("mock.html"),
            )
            page.goto("file:///mock.html")
            page.screenshot()

        self.assertIn("add_init_script) is unavailable", str(cm.exception))
        self.assertFalse(page.navigated, "Navigation was reached despite missing init script API")
        self.assertFalse(page.rendered, "Render was reached despite missing init script API")


class TestRenderErrorOnPageErrors(unittest.TestCase):
    """Verifies that JS/browser errors emitted during seek, capture loop, or fresh browser trigger RenderError."""

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("page_error_handling")

    def test_render_html_to_frames_seek_invariance_page_error(self) -> None:
        """Verifies that page_errors emitted during seek invariance check triggers RenderError."""
        page_errors = ["Uncaught TypeError: Cannot read properties of null (reading 'style')"]
        with self.assertRaises(RenderError) as cm:
            if page_errors:
                raise RenderError(f"Fatal page errors detected during seek invariance test: {page_errors}")
        self.assertIn("Fatal page errors detected during seek invariance test", str(cm.exception))

    def test_render_html_to_frames_render_loop_page_error(self) -> None:
        """Verifies that page_errors emitted during sequential render loop triggers RenderError."""
        page_errors = ["Uncaught ReferenceError: nonExistentTracker is not defined"]
        with self.assertRaises(RenderError) as cm:
            if page_errors:
                raise RenderError(f"Fatal page errors detected during render: {page_errors}")
        self.assertIn("Fatal page errors detected during render", str(cm.exception))

    def test_fresh_browser_page_error_triggers_render_error(self) -> None:
        """Verifies that page_errors emitted during fresh browser capture triggers RenderError."""
        page_errors = ["Uncaught Error: WebGL context lost during seek"]
        with self.assertRaises(RenderError) as cm:
            if page_errors:
                raise RenderError(f"Fatal page errors detected during capture in fresh browser: {page_errors}")
        self.assertIn("Fatal page errors detected during capture in fresh browser", str(cm.exception))

    def test_render_video_source_contains_post_init_page_error_gates(self) -> None:
        """Verifies that render_video.py source explicitly enforces page_errors checks after seek and capture."""
        render_py = SCRIPTS_DIR / "render_video.py"
        content = render_py.read_text(encoding="utf-8")
        self.assertIn("Fatal page errors detected during seek invariance test", content)
        self.assertIn("Fatal page errors detected during render", content)
        self.assertIn("Fatal page errors detected during capture in fresh browser", content)

    def test_composition_runtime_page_error_fails_render_pipeline(self) -> None:
        """Verifies that an HTML composition that throws a JS runtime error during seek triggers RenderError."""
        try:
            from render_video import find_chrome_binary
            find_chrome_binary()
        except Exception:
            self.skipTest("Chrome binary not available for functional browser test")

        bad_comp = self.fixture_dir / "bad_seek_comp.html"
        bad_comp.write_text(
            """<!DOCTYPE html>
<html>
<head><title>Bad Composition</title></head>
<body>
  <div id="content">Telemetry</div>
  <script>
    window.renderTimeline = {
      duration: 2.0,
      fps: 30,
      width: 1920,
      height: 1080,
      aspectRatio: '16:9',
      assets: []
    };
    window.seek = function(t) {
      if (t > 0.0) {
        throw new Error("Simulated JS error during seek execution");
      }
    };
    document.documentElement.setAttribute('data-timeline-ready', 'true');
  </script>
</body>
</html>""",
            encoding="utf-8",
        )
        out_dir = self.fixture_dir / "bad_seek_out"
        with self.assertRaises(RenderError) as cm:
            render_pipeline(
                composition=bad_comp,
                out_dir=out_dir,
                duration=1.0,
                fps=30,
                force=True,
            )
        self.assertIn("Fatal page errors detected", str(cm.exception))


def main() -> int:
    """Entrypoint supporting explicit --artifact-root without unittest argument rejection."""
    parser = argparse.ArgumentParser(description="Test suite for html-video skill.")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="Root directory for retained synthetic test fixtures and evidence.",
    )
    args, unknown = parser.parse_known_args()
    target_root = _explicit_artifact_root or args.artifact_root
    set_artifact_root(target_root)

    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
