#!/usr/bin/env python3
"""
Frame-accurate, deterministic HTML video renderer.

Renders HTML/CSS/SVG compositions using headless Google Chrome and FFmpeg, with
strict network isolation, local asset pinning, deterministic seek-order verification,
cross-browser repeat determinism, ffprobe technical validation, and contact-sheet evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Ensure bytecode is never written to disk
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


class RenderError(RuntimeError):
    """Base error for html-video rendering issues."""
    pass


class SecurityError(RenderError):
    """Raised when security boundaries or isolation policies are violated."""
    pass


class ValidationError(RenderError):
    """Raised when technical validation or timeline contract checks fail."""
    pass


# -----------------------------------------------------------------------------
# Process & Isolation Utilities
# -----------------------------------------------------------------------------

def run_hidden_subprocess(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """
    Executes a subprocess with hidden window flags on Windows and no shell execution.
    Complies with workspace rules against unsolicited console windows.
    """
    creationflags = 0
    startupinfo = None

    if sys.platform == "win32":
        # CREATE_NO_WINDOW = 0x08000000
        creationflags = 0x08000000
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    return subprocess.run(
        cmd,
        creationflags=creationflags,
        startupinfo=startupinfo,
        shell=False,
        text=True,
        capture_output=True,
        check=False,
        **kwargs,
    )


def find_chrome_binary(custom_path: str | None = None) -> str:
    """Locates Google Chrome binary with fallback priority."""
    candidates = []
    if custom_path:
        candidates.append(Path(custom_path))

    env_chrome = os.environ.get("CHROME_BIN") or os.environ.get("CHROME_PATH")
    if env_chrome:
        candidates.append(Path(env_chrome))

    candidates.extend([
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    ])

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    which_chrome = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    if which_chrome:
        return str(Path(which_chrome).resolve())

    raise SecurityError(
        "Google Chrome binary not found. Expected at "
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe or set CHROME_BIN."
    )


def require_tool(tool_name: str) -> str:
    """Verifies that an external binary exists on PATH and returns its path."""
    tool_path = shutil.which(tool_name)
    if not tool_path:
        raise ValidationError(f"Required binary '{tool_name}' not found on PATH.")
    return tool_path


def get_runtime_versions(
    browser_bin: str | None = None,
    browser_version: str | None = None,
) -> dict[str, Any]:
    """
    Collects exact runtime versions for Python, platform, Chrome, Playwright, FFmpeg, and FFprobe.
    Captures executable versions to ensure reproducibility of reported determinism tolerances.
    """
    versions: dict[str, Any] = {
        "python": sys.version,
        "platform": sys.platform,
        "playwright": "unknown",
        "playwright_version": "unknown",
        "browser_binary": browser_bin,
        "browser_version": browser_version or "unknown",
        "ffmpeg": "unknown",
        "ffmpeg_version": "unknown",
        "ffprobe": "unknown",
        "ffprobe_version": "unknown",
        "pillow": "unknown",
        "pillow_version": "unknown",
    }

    # Playwright version
    try:
        import playwright
        pw_ver = getattr(playwright, "__version__", "unknown")
        versions["playwright"] = pw_ver
        versions["playwright_version"] = pw_ver
    except Exception:
        pass
    if versions["playwright"] == "unknown":
        try:
            import importlib.metadata
            pw_ver = importlib.metadata.version("playwright")
            versions["playwright"] = pw_ver
            versions["playwright_version"] = pw_ver
        except Exception:
            pass

    # Pillow version
    try:
        import PIL
        pil_ver = getattr(PIL, "__version__", "unknown")
        versions["pillow"] = pil_ver
        versions["pillow_version"] = pil_ver
    except Exception:
        pass

    # Browser version via CLI / file version if not already supplied from Playwright browser.version
    if (not browser_version or browser_version == "unknown") and browser_bin:
        resolved_bin = Path(browser_bin)
        if resolved_bin.is_file():
            cli_success = False
            try:
                res = run_hidden_subprocess([str(resolved_bin), "--version"])
                if res.returncode == 0 and res.stdout.strip():
                    versions["browser_version"] = res.stdout.strip()
                    cli_success = True
            except Exception:
                pass

            if not cli_success and sys.platform == "win32":
                try:
                    escaped_bin = str(resolved_bin).replace("'", "''")
                    ps_cmd = [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-WindowStyle",
                        "Hidden",
                        "-Command",
                        f"(Get-Item -LiteralPath '{escaped_bin}').VersionInfo.ProductVersion",
                    ]
                    ps_res = run_hidden_subprocess(ps_cmd)
                    if ps_res.returncode == 0 and ps_res.stdout.strip():
                        versions["browser_version"] = ps_res.stdout.strip()
                except Exception:
                    pass

    # FFmpeg version
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        res = run_hidden_subprocess(["ffmpeg", "-version"])
        if res.returncode == 0 and res.stdout:
            first_line = res.stdout.splitlines()[0].strip()
            versions["ffmpeg"] = first_line
            versions["ffmpeg_version"] = first_line

    # FFprobe version
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        res = run_hidden_subprocess(["ffprobe", "-version"])
        if res.returncode == 0 and res.stdout:
            first_line = res.stdout.splitlines()[0].strip()
            versions["ffprobe"] = first_line
            versions["ffprobe_version"] = first_line

    return versions


# -----------------------------------------------------------------------------
# Security & Path Gates
# -----------------------------------------------------------------------------

def validate_safe_path(target_path: Path, output_root: Path, allow_overwrite: bool = False) -> Path:
    """
    Validates that target_path resolves strictly within output_root,
    contains no path traversal or shell characters, and does not overwrite existing files.
    """
    resolved_root = output_root.resolve()
    resolved_target = target_path.resolve()

    # Boundary check
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        raise SecurityError(f"Target path '{target_path}' escapes output root '{output_root}'.")

    # Character sanity check
    raw_str = str(target_path)
    forbidden = [";", "&", "|", "`", "$", "<", ">", "\n", "\r"]
    for char in forbidden:
        if char in raw_str:
            raise SecurityError(f"Target path '{target_path}' contains illegal shell character '{char}'.")

    # Overwrite check
    if resolved_target.exists() and not allow_overwrite:
        raise SecurityError(
            f"Target file '{resolved_target}' already exists. Refusing overwrite without explicit force."
        )

    return resolved_target


def compute_sha256(file_path: Path) -> str:
    """Calculates SHA256 checksum of a file."""
    if not file_path.is_file():
        return ""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def extract_declared_assets(composition_path: Path) -> list[str]:
    """Extracts declared relative asset paths from composition HTML file."""
    if not composition_path.is_file():
        return []
    content = composition_path.read_text(encoding="utf-8", errors="ignore")
    assets: list[str] = []
    # Match window.renderTimeline assets array: assets: ['assets/font.woff2', ...]
    m = re.search(r"assets\s*:\s*\[(.*?)\]", content, re.DOTALL)
    if m:
        for item in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)):
            cleaned = item.strip()
            if cleaned:
                assets.append(cleaned)
    return assets


def resolve_and_pin_assets(
    composition_path: Path,
    admitted_assets: list[str] | None = None,
) -> tuple[set[Path], dict[str, dict[str, Any]]]:
    """
    Extracts declared assets from composition plus any additional admitted assets.
    Validates containment within composition directory and existence on disk.
    Computes SHA256 hashes and builds the pinned assets manifest.
    """
    comp_resolved = composition_path.resolve()
    comp_dir = comp_resolved.parent

    declared_set = set(extract_declared_assets(comp_resolved))
    if admitted_assets:
        declared_set.update(admitted_assets)

    admitted_paths: set[Path] = set()
    manifest_entries: dict[str, dict[str, Any]] = {}

    for rel_asset in sorted(declared_set):
        resolved_asset = (comp_dir / rel_asset).resolve()
        try:
            resolved_asset.relative_to(comp_dir)
        except ValueError:
            raise SecurityError(
                f"Declared asset '{rel_asset}' escapes composition directory '{comp_dir}'."
            )

        if not resolved_asset.is_file():
            raise ValidationError(f"Declared asset not found on disk: {resolved_asset}")

        admitted_paths.add(resolved_asset)
        manifest_entries[rel_asset] = {
            "path": str(resolved_asset),
            "sha256": compute_sha256(resolved_asset),
            "size_bytes": resolved_asset.stat().st_size,
        }

    return admitted_paths, manifest_entries


def classify_request_url(
    url: str,
    composition_path: Path,
    admitted_paths: set[Path] | None = None,
) -> tuple[str, str]:
    """
    Classifies a request URL against strict network and asset containment boundaries.
    Returns (decision, reason):
      decision is 'allow', 'block', or 'deny'.
      reason describes the outcome (e.g. 'composition_root', 'admitted_asset',
      'unpinned_local_file', 'outside_composition_boundary', 'data_uri', 'blockedbyclient').
    """
    parsed = urllib.parse.urlparse(url)

    # 1. Block remote protocols immediately
    if parsed.scheme in ("http", "https", "ws", "wss", "ftp"):
        return ("block", "blockedbyclient")

    # 2. Inlined data URIs are permitted
    if parsed.scheme == "data":
        return ("allow", "data_uri")

    # 3. File URLs: constrain strictly to composition root and admitted pinned assets
    if parsed.scheme == "file":
        req_path = Path(urllib.request.url2pathname(parsed.path)).resolve()
        comp_resolved = composition_path.resolve()

        if req_path == comp_resolved:
            return ("allow", "composition_root")

        if admitted_paths and req_path in admitted_paths:
            return ("allow", "admitted_asset")

        # Unpinned file inside composition folder or file outside composition folder
        comp_dir = comp_resolved.parent
        try:
            req_path.relative_to(comp_dir)
            return ("deny", "unpinned_local_file")
        except ValueError:
            return ("deny", "outside_composition_boundary")

    # Any other unknown or unsafe protocol is blocked
    return ("block", "blockedbyclient")


SECURITY_INIT_SCRIPT = """
if (typeof WebSocket !== 'undefined') {
    window.WebSocket = class {
        constructor() {
            throw new Error('WebSocket connection blocked by html-video security policy');
        }
    };
}
if (typeof SharedWorker !== 'undefined') {
    window.SharedWorker = class {
        constructor() {
            throw new Error('SharedWorker blocked by html-video security policy');
        }
    };
}
if (navigator.serviceWorker) {
    try {
        navigator.serviceWorker.register = function() {
            return Promise.reject(new Error('ServiceWorker registration blocked by html-video security policy'));
        };
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'serviceWorker', {
            get: () => undefined,
            configurable: false
        });
    } catch (e) {}
}
"""


def setup_browser_page_isolation(
    context: Any,
    page: Any,
    composition_path: Path,
    admitted_paths: set[Path] | None = None,
    blocked_requests: list[str] | None = None,
) -> None:
    """
    Applies strict security and network isolation to a Playwright context and page.
    Installs init-script overrides, ServiceWorker and WebSocket listeners, route interception,
    and pre-network WebSocket routing via context.route_web_socket.

    Must fail closed before any navigation if context HTTP or pre-network WebSocket
    route registration is unavailable or raises. Never silently swallows route registration
    failures or downgrades context isolation to page-only.
    """
    if blocked_requests is None:
        blocked_requests = []

    # 1. Init script security overrides
    if not hasattr(context, "add_init_script") or not callable(getattr(context, "add_init_script", None)):
        raise SecurityError(
            "Browser context init script registration (context.add_init_script) is unavailable; failing closed."
        )
    try:
        context.add_init_script(SECURITY_INIT_SCRIPT)
    except Exception as exc:
        raise SecurityError(f"Context init script registration failed: {exc}") from exc

    # 2. Pre-network WebSocket isolation at BrowserContext level (Playwright 1.48+)
    # Closes WebSocketRoute immediately without calling connect_to_server().
    # Fails closed if context.route_web_socket is unavailable or raises.
    if not hasattr(context, "route_web_socket") or not callable(getattr(context, "route_web_socket", None)):
        raise SecurityError(
            "Context pre-network WebSocket route registration is unavailable (route_web_socket missing); failing closed."
        )

    def handle_ws_route(ws_route: Any) -> None:
        url = getattr(ws_route, "url", "unknown_ws")
        blocked_requests.append(f"ws_route:{url}")
        try:
            ws_route.close(code=1008, reason="WebSocket blocked by html-video security policy")
        except Exception:
            pass

    try:
        context.route_web_socket("**/*", handle_ws_route)
    except Exception as exc:
        raise SecurityError(
            f"Context pre-network WebSocket route registration failed: {exc}"
        ) from exc

    # 3. Defense-in-depth: Page and Context level websocket and serviceworker event listeners
    def handle_websocket(ws: Any) -> None:
        blocked_requests.append(f"ws:{ws.url}")
        try:
            ws.close()
        except Exception:
            pass

    if hasattr(page, "on") and callable(getattr(page, "on", None)):
        try:
            page.on("websocket", handle_websocket)
        except Exception:
            pass

    if hasattr(context, "on") and callable(getattr(context, "on", None)):
        try:
            context.on("websocket", handle_websocket)
        except Exception:
            pass
        try:
            context.on("serviceworker", lambda sw: blocked_requests.append(f"serviceworker:{sw.url}"))
        except Exception:
            pass

    # 4. HTTP / Local file routing at BrowserContext level
    # Fails closed if context.route is unavailable or raises. Never downgrades to page-only routing.
    if not hasattr(context, "route") or not callable(getattr(context, "route", None)):
        raise SecurityError(
            "Context HTTP route registration is unavailable (context.route missing); failing closed."
        )

    def handle_route(route: Any) -> None:
        url = route.request.url
        decision, reason = classify_request_url(
            url=url,
            composition_path=composition_path,
            admitted_paths=admitted_paths,
        )
        if decision == "allow":
            route.continue_()
        elif decision == "deny":
            blocked_requests.append(url)
            route.abort("accessdenied")
        else:  # "block"
            blocked_requests.append(url)
            route.abort("blockedbyclient")

    try:
        context.route("**/*", handle_route)
    except Exception as exc:
        raise SecurityError(
            f"Context HTTP route registration failed: {exc}"
        ) from exc


def verify_probed_video(
    probe: dict[str, Any],
    expected_w: int,
    expected_h: int,
    expected_duration: float = 5.0,
    expected_fps: int = 30,
    expected_nb_frames: int = 150,
    epsilon: float = 0.05,
) -> None:
    """Verifies that probed video metadata matches expected dimensions, duration, FPS, and frame count."""
    if probe.get("width") != expected_w or probe.get("height") != expected_h:
        raise ValidationError(
            f"Dimensions mismatch: expected {expected_w}x{expected_h}, got {probe.get('width')}x{probe.get('height')}"
        )
    dur = probe.get("duration_sec", 0.0)
    allowed_dur_delta = (1.0 / expected_fps) + epsilon
    if abs(dur - expected_duration) > allowed_dur_delta:
        raise ValidationError(
            f"Duration mismatch: expected {expected_duration}s (±{allowed_dur_delta:.3f}s), got {dur}s"
        )
    expected_rate = f"{expected_fps}/1"
    if probe.get("r_frame_rate") != expected_rate:
        raise ValidationError(
            f"Frame rate mismatch: expected {expected_rate}, got {probe.get('r_frame_rate')}"
        )
    nb_frames = probe.get("nb_frames")
    if nb_frames is not None and nb_frames != expected_nb_frames:
        raise ValidationError(
            f"Frame count mismatch: expected {expected_nb_frames} frames, got {nb_frames}"
        )


def validate_composition_geometry(
    timeline_meta: dict[str, Any],
    requested_aspect: str,
    target_w: int | None = None,
    target_h: int | None = None,
) -> None:
    """
    Validates consistency between requested aspect ratio / viewport dimensions
    and the composition's declared renderTimeline metadata.
    Raises ValidationError on mismatch.
    """
    comp_aspect = timeline_meta.get("aspectRatio")
    comp_w = timeline_meta.get("width")
    comp_h = timeline_meta.get("height")

    if comp_aspect and comp_aspect != requested_aspect:
        raise ValidationError(
            f"Composition geometry mismatch: requested aspect '{requested_aspect}', but composition "
            f"renderTimeline declared aspectRatio '{comp_aspect}' ({comp_w}x{comp_h})."
        )

    if comp_w and comp_h:
        if (target_w and comp_w != target_w) or (target_h and comp_h != target_h):
            raise ValidationError(
                f"Composition geometry mismatch: target viewport is {target_w}x{target_h} ({requested_aspect}), "
                f"but composition renderTimeline declared {comp_w}x{comp_h}."
            )


# -----------------------------------------------------------------------------
# Audio Technical Validation
# -----------------------------------------------------------------------------

def validate_wav_audio(wav_path: Path) -> dict[str, Any]:
    """
    Strict technical validation of an uncompressed WAV audio file.
    Validates RIFF header, PCM format code, channels, sample rate, and calculates duration.
    """
    if not wav_path.is_file():
        raise ValidationError(f"Audio file not found: {wav_path}")

    if wav_path.suffix.lower() != ".wav":
        raise ValidationError(f"Audio must be a .wav file: {wav_path}")

    with open(wav_path, "rb") as f:
        header = f.read(44)
        if len(header) < 44:
            raise ValidationError(f"WAV file header too short ({len(header)} bytes): {wav_path}")

        riff, file_size, wave, fmt_id, fmt_size = struct.unpack("<4sI4s4sI", header[:20])
        if riff != b"RIFF" or wave != b"WAVE":
            raise ValidationError(f"Invalid RIFF/WAVE header in {wav_path}")

        if fmt_id != b"fmt ":
            raise ValidationError(f"Expected 'fmt ' chunk, got {fmt_id!r} in {wav_path}")

        audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack(
            "<HHIIHH", header[20:36]
        )

        if audio_format != 1:  # 1 = PCM
            raise ValidationError(f"Unsupported audio format {audio_format} (only PCM=1 is supported)")

        if channels not in (1, 2):
            raise ValidationError(f"Unsupported channel count {channels} (expected 1 or 2)")

        if sample_rate not in (44100, 48000, 96000):
            raise ValidationError(f"Non-standard sample rate: {sample_rate} Hz")

        if bits_per_sample not in (8, 16, 24, 32):
            raise ValidationError(
                f"Unsupported or invalid bits per sample: {bits_per_sample} (expected 8, 16, 24, or 32)"
            )

        expected_block_align = channels * (bits_per_sample // 8)
        if block_align != expected_block_align:
            raise ValidationError(
                f"Invalid block align: {block_align} (expected {expected_block_align} for {channels} ch, {bits_per_sample} bits)"
            )

        # Search for data chunk
        f.seek(20 + fmt_size)
        data_chunk_found = False
        data_size = 0
        while True:
            chunk_hdr = f.read(8)
            if len(chunk_hdr) < 8:
                break
            c_id, c_size = struct.unpack("<4sI", chunk_hdr)
            if c_id == b"data":
                data_chunk_found = True
                data_size = c_size
                break
            f.seek(c_size, os.SEEK_CUR)

        if not data_chunk_found or data_size <= 0:
            raise ValidationError(f"No valid 'data' chunk found in WAV {wav_path}")

        bytes_per_sample = bits_per_sample // 8
        frame_size = channels * bytes_per_sample
        total_frames = data_size // frame_size
        duration_sec = total_frames / sample_rate

    return {
        "valid": True,
        "format": "PCM",
        "channels": channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits_per_sample,
        "duration_sec": round(duration_sec, 3),
        "data_bytes": data_size,
    }


def create_synthetic_wav(
    path: Path,
    duration_sec: float = 5.0,
    sample_rate: int = 48000,
    channels: int = 2,
    bits_per_sample: int = 16,
    frequency: float = 440.0,
) -> Path:
    """Generates a valid PCM WAV audio file with a synthetic sine wave tone for technical tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_per_sample = bits_per_sample // 8
    frame_size = channels * bytes_per_sample
    total_frames = int(sample_rate * duration_sec)
    data_size = total_frames * frame_size
    byte_rate = sample_rate * frame_size
    file_size = 36 + data_size

    with open(path, "wb") as f:
        # RIFF Header
        f.write(b"RIFF")
        f.write(struct.pack("<I", file_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))  # PCM
        f.write(struct.pack("<H", channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", frame_size))
        f.write(struct.pack("<H", bits_per_sample))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))

        # Modulated sine wave sample stream
        amplitude = 8000
        for i in range(total_frames):
            t = i / sample_rate
            val = int(amplitude * math.sin(2.0 * math.pi * frequency * t))
            sample = struct.pack("<h", val)
            for _ in range(channels):
                f.write(sample)

    return path


# -----------------------------------------------------------------------------
# Frame-Accurate Playwright Engine
# -----------------------------------------------------------------------------

def render_html_to_frames(
    composition_path: Path,
    frames_dir: Path,
    aspect: str = "16:9",
    override_width: int | None = None,
    override_height: int | None = None,
    override_fps: int | None = None,
    override_duration: float | None = None,
    admitted_assets: list[str] | None = None,
    chrome_path: str | None = None,
) -> dict[str, Any]:
    """
    Renders the HTML composition frame-by-frame via headless Chrome using Playwright.
    Enforces strict network isolation, explicit local asset pinning, font readiness,
    service worker and WebSocket suppression, and seek-order invariance determinism testing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RenderError("Python 'playwright' package is required but not installed.")

    composition_resolved = composition_path.resolve()
    if not composition_resolved.is_file():
        raise ValidationError(f"Composition file not found: {composition_path}")

    composition_dir = composition_resolved.parent
    frames_dir.mkdir(parents=True, exist_ok=True)

    browser_bin = find_chrome_binary(chrome_path)

    # Resolve and pin explicitly declared assets
    admitted_asset_paths, pinned_assets_manifest = resolve_and_pin_assets(
        composition_resolved, admitted_assets
    )

    # Track isolated events and errors
    blocked_requests: list[str] = []
    page_errors: list[str] = []
    console_errors: list[str] = []

    # Aspect ratio defaults
    if aspect == "9:16":
        default_w, default_h = 1080, 1920
    elif aspect == "1:1":
        default_w, default_h = 1080, 1080
    elif aspect == "16:9":
        default_w, default_h = 1920, 1080
    else:
        raise ValidationError(f"Unsupported aspect ratio: '{aspect}'. Supported values: '16:9', '9:16', '1:1'.")

    target_w = override_width or default_w
    target_h = override_height or default_h

    # URL with parameters
    file_url = f"file:///{composition_resolved.as_posix()}?aspect={aspect}&w={target_w}&h={target_h}"

    # Hardened Chrome launch arguments: native OS sandbox is enforced
    # Uses verifiable Chromium feature flag --disable-features=ServiceWorker,SharedWorker
    launch_args = [
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

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=browser_bin,
            headless=True,
            args=launch_args,
            chromium_sandbox=True,
        )
        browser_version = browser.version

        context = browser.new_context(
            viewport={"width": target_w, "height": target_h},
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            java_script_enabled=True,
            bypass_csp=False,
            service_workers="block",
        )

        page = context.new_page()

        # Apply multi-layered security and network isolation
        setup_browser_page_isolation(
            context=context,
            page=page,
            composition_path=composition_resolved,
            admitted_paths=admitted_asset_paths,
            blocked_requests=blocked_requests,
        )

        # Event Listeners
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        # Navigate
        page.goto(file_url, wait_until="domcontentloaded", timeout=15000)

        # Gate 1: Wait for document.readyState === 'complete'
        page.wait_for_function("() => document.readyState === 'complete'", timeout=15000)

        # Gate 2: Wait for font readiness
        page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()")

        # Inspect loaded fonts, status, and compute font fingerprint
        fonts_info = page.evaluate("""
            () => {
                const list = [];
                if (document.fonts) {
                    document.fonts.forEach(f => {
                        list.push({
                            family: f.family,
                            status: f.status,
                            weight: f.weight,
                            style: f.style
                        });
                    });
                }
                return list;
            }
        """)

        for f in fonts_info:
            if f.get("status") == "error":
                browser.close()
                raise RenderError(f"Font loading failed: {f}")

        # Compute font hash: if any font files are pinned, record their binary SHA256;
        # otherwise record the FontFaceSet metadata hash
        pinned_font_hashes = [
            meta["sha256"]
            for asset_name, meta in pinned_assets_manifest.items()
            if any(asset_name.lower().endswith(ext) for ext in (".ttf", ".woff", ".woff2", ".otf"))
        ]
        if pinned_font_hashes:
            font_hash = pinned_font_hashes[0] if len(pinned_font_hashes) == 1 else hashlib.sha256("".join(pinned_font_hashes).encode("utf-8")).hexdigest()
        else:
            font_hash = hashlib.sha256(json.dumps(fonts_info, sort_keys=True).encode("utf-8")).hexdigest()

        # Gate 3: Verify image assets readiness: fail on missing or broken images
        broken_images = page.evaluate("""
            () => {
                const imgs = Array.from(document.querySelectorAll('img'));
                const broken = [];
                for (const img of imgs) {
                    if (!img.complete || img.naturalWidth === 0) {
                        broken.push(img.src);
                    }
                }
                return broken;
            }
        """)

        if broken_images:
            browser.close()
            raise RenderError(f"Missing or broken image assets detected: {broken_images}")

        # Gate 4: Wait for timeline contract hooks
        try:
            page.wait_for_function(
                "() => window.renderTimeline !== undefined || window.seek !== undefined",
                timeout=15000,
            )
        except Exception as exc:
            browser.close()
            raise RenderError(
                f"Timeline contract hooks (renderTimeline or seek) not found within timeout: {exc}"
            )

        # Gate 5: Wait for explicit timeline readiness marker data-timeline-ready === 'true'
        try:
            page.wait_for_function(
                "() => document.documentElement.getAttribute('data-timeline-ready') === 'true'",
                timeout=15000,
            )
        except Exception as exc:
            browser.close()
            raise RenderError(
                f"Timeline readiness marker data-timeline-ready='true' not satisfied within timeout: {exc}"
            )

        # Fail early if page errors occurred during initialization
        if page_errors:
            browser.close()
            raise RenderError(f"Fatal page errors detected on load: {page_errors}")

        # Read timeline contract properties
        timeline_meta = page.evaluate("""
            () => {
                const rt = window.renderTimeline || {};
                return {
                    duration: rt.duration || 5.0,
                    fps: rt.fps || 30,
                    width: rt.width || window.innerWidth,
                    height: rt.height || window.innerHeight,
                    aspectRatio: rt.aspectRatio || '16:9',
                    assets: rt.assets || []
                };
            }
        """)

        # Validate geometry consistency between request and composition declaration
        try:
            validate_composition_geometry(
                timeline_meta=timeline_meta,
                requested_aspect=aspect,
                target_w=target_w,
                target_h=target_h,
            )
        except ValidationError:
            browser.close()
            raise

        duration = override_duration or timeline_meta.get("duration", 5.0)
        fps = override_fps or timeline_meta.get("fps", 30)
        total_frames = int(math.ceil(duration * fps))

        # Perform Seek-Order Invariance Test (Determinism Audit)
        test_t = min(1.0, duration * 0.5)
        try:
            page.evaluate(f"window.seek({test_t})")
        except Exception as exc:
            page_errors.append(f"Seek evaluation error at t={test_t}: {exc}")

        # Re-check for page errors emitted during seek invariance check
        if page_errors:
            browser.close()
            raise RenderError(f"Fatal page errors detected during seek invariance test: {page_errors}")

        sample_forward_bytes = page.screenshot(type="png")

        # Jump forward to end then directly back to test_t
        try:
            page.evaluate(f"window.seek({duration})")
        except Exception as exc:
            page_errors.append(f"Seek evaluation error at t={duration}: {exc}")
        try:
            page.evaluate(f"window.seek({test_t})")
        except Exception as exc:
            page_errors.append(f"Seek evaluation error at t={test_t}: {exc}")

        # Re-check for page errors emitted during seek invariance check
        if page_errors:
            browser.close()
            raise RenderError(f"Fatal page errors detected during seek invariance test: {page_errors}")

        sample_reverse_bytes = page.screenshot(type="png")

        determinism_metric = compare_frame_bytes(sample_forward_bytes, sample_reverse_bytes)
        if not determinism_metric.get("tolerance_passed", False):
            browser.close()
            raise RenderError(f"Seek-order determinism check failed: MAE={determinism_metric.get('mae')}")

        # Sequentially render all frames
        frame_files: list[Path] = []
        for i in range(total_frames):
            t = i / fps
            try:
                page.evaluate(f"window.seek({t})")
            except Exception as exc:
                page_errors.append(f"Seek evaluation error at t={t}: {exc}")
                break

            if page_errors:
                break

            frame_path = frames_dir / f"frame_{i:06d}.png"
            try:
                page.screenshot(path=str(frame_path), type="png")
            except Exception as exc:
                page_errors.append(f"Screenshot error at t={t}: {exc}")
                break
            frame_files.append(frame_path)

        # Re-check for page errors emitted during seek and screenshot capture
        if page_errors:
            browser.close()
            raise RenderError(f"Fatal page errors detected during render: {page_errors}")

        browser.close()

    return {
        "total_frames": total_frames,
        "duration": duration,
        "fps": fps,
        "width": target_w,
        "height": target_h,
        "aspect_ratio": aspect,
        "frames_dir": str(frames_dir),
        "frame_files": [str(f) for f in frame_files],
        "blocked_requests": blocked_requests,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "determinism_metric": determinism_metric,
        "browser_binary": browser_bin,
        "browser_version": browser_version,
        "font_hash": font_hash,
        "fonts_info": fonts_info,
        "pinned_assets": pinned_assets_manifest,
    }


def compare_frame_bytes(bytes_a: bytes, bytes_b: bytes) -> dict[str, Any]:
    """
    Compares two PNG frame byte buffers using Pillow to quantify pixel deviation.
    Reports Mean Absolute Error (MAE), Peak Signal-to-Noise Ratio (PSNR), and tolerance.
    """
    import io
    try:
        from PIL import Image, ImageChops
    except ImportError:
        exact = bytes_a == bytes_b
        return {
            "exact_byte_match": exact,
            "max_channel_diff": 0 if exact else 255,
            "mae": 0.0 if exact else 1.0,
            "tolerance": 0.001,
            "tolerance_passed": exact,
        }

    im_a = Image.open(io.BytesIO(bytes_a)).convert("RGB")
    im_b = Image.open(io.BytesIO(bytes_b)).convert("RGB")

    diff = ImageChops.difference(im_a, im_b)
    stat = diff.getextrema()
    max_diff = max(s[1] for s in stat)

    w, h = im_a.size
    total_pixels = w * h * 3
    hist = diff.histogram()
    diff_sum = 0
    for ch in range(3):
        for val, count in enumerate(hist[ch * 256 : (ch + 1) * 256]):
            diff_sum += val * count
    mae = diff_sum / total_pixels

    tolerance = 0.001
    passed = mae <= tolerance

    return {
        "exact_byte_match": bytes_a == bytes_b,
        "max_channel_diff": max_diff,
        "mae": round(mae, 6),
        "tolerance": tolerance,
        "tolerance_passed": passed,
    }


def verify_fresh_browser_determinism(
    composition_path: Path,
    evidence_dir: Path,
    aspect: str = "16:9",
    test_t: float = 2.5,
    admitted_assets: list[str] | None = None,
    chrome_path: str | None = None,
) -> dict[str, Any]:
    """
    Renders the exact same frame in two completely separate, independent headless Chrome instances
    to prove cross-browser repeated-frame determinism under declared tolerance without in-memory cache pollution.
    Enforces the exact same security, network isolation, and asset boundaries as the primary render path.
    """
    from playwright.sync_api import sync_playwright

    browser_bin = find_chrome_binary(chrome_path)
    composition_resolved = composition_path.resolve()
    if aspect == "9:16":
        target_w, target_h = 1080, 1920
    elif aspect == "1:1":
        target_w, target_h = 1080, 1080
    else:
        target_w, target_h = 1920, 1080
    file_url = f"file:///{composition_resolved.as_posix()}?aspect={aspect}&w={target_w}&h={target_h}"

    admitted_asset_paths, _ = resolve_and_pin_assets(composition_resolved, admitted_assets)

    launch_args = [
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

    def capture_single_frame() -> bytes:
        blocked_reqs: list[str] = []
        page_errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=browser_bin,
                headless=True,
                args=launch_args,
                chromium_sandbox=True,
            )
            context = browser.new_context(
                viewport={"width": target_w, "height": target_h},
                service_workers="block",
            )
            page = context.new_page()

            # Apply identical multi-layered isolation
            setup_browser_page_isolation(
                context=context,
                page=page,
                composition_path=composition_resolved,
                admitted_paths=admitted_asset_paths,
                blocked_requests=blocked_reqs,
            )

            page.on("pageerror", lambda err: page_errors.append(str(err)))

            page.goto(file_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_function("() => document.readyState === 'complete'", timeout=15000)
            page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()")
            page.wait_for_function("() => window.seek !== undefined", timeout=15000)
            try:
                page.wait_for_function(
                    "() => document.documentElement.getAttribute('data-timeline-ready') === 'true'",
                    timeout=15000,
                )
            except Exception as exc:
                browser.close()
                raise RenderError(
                    f"Readiness marker data-timeline-ready='true' timeout in fresh browser: {exc}"
                )

            if page_errors:
                browser.close()
                raise RenderError(f"Fatal page errors detected on load in fresh browser: {page_errors}")

            try:
                page.evaluate(f"window.seek({test_t})")
            except Exception as exc:
                page_errors.append(f"Seek evaluation error at t={test_t} in fresh browser: {exc}")

            if page_errors:
                browser.close()
                raise RenderError(f"Fatal page errors detected during capture in fresh browser: {page_errors}")

            try:
                frame_bytes = page.screenshot(type="png")
            except Exception as exc:
                page_errors.append(f"Screenshot error in fresh browser: {exc}")

            if page_errors:
                browser.close()
                raise RenderError(f"Fatal page errors detected during capture in fresh browser: {page_errors}")

            browser.close()
            return frame_bytes

    frame_bytes_1 = capture_single_frame()
    frame_bytes_2 = capture_single_frame()

    evidence_dir.mkdir(parents=True, exist_ok=True)
    frame_file_1 = evidence_dir / f"fresh_browser_frame_1_t{test_t:.1f}.png"
    frame_file_2 = evidence_dir / f"fresh_browser_frame_2_t{test_t:.1f}.png"
    frame_file_1.write_bytes(frame_bytes_1)
    frame_file_2.write_bytes(frame_bytes_2)

    det_metric = compare_frame_bytes(frame_bytes_1, frame_bytes_2)
    det_metric["frame_1_path"] = str(frame_file_1)
    det_metric["frame_2_path"] = str(frame_file_2)
    det_metric["test_t"] = test_t

    metric_file = evidence_dir / "fresh_browser_determinism.json"
    with open(metric_file, "w", encoding="utf-8") as f:
        json.dump(det_metric, f, indent=2)

    return det_metric


# -----------------------------------------------------------------------------
# FFmpeg & Technical Encoding
# -----------------------------------------------------------------------------

def encode_frames_to_mp4(
    frames_dir: Path,
    output_mp4: Path,
    fps: int,
    total_frames: int | None = None,
    audio_path: Path | None = None,
    allow_overwrite: bool = False,
) -> Path:
    """
    Encodes PNG frame sequence into a web-optimized MP4 using hidden FFmpeg subprocess.
    Optionally muxes an explicit validated WAV audio file.
    When total_frames is provided, passes -vframes to strictly limit encoding to the active render,
    preventing any stale or higher-numbered frames in frames_dir from leaking into the output MP4.
    """
    require_tool("ffmpeg")

    if output_mp4.exists() and not allow_overwrite:
        raise SecurityError(f"Output MP4 already exists: {output_mp4}. Use --force to overwrite.")

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    frame_pattern = str(frames_dir / "frame_%06d.png")

    cmd = [
        "ffmpeg",
        "-y" if allow_overwrite else "-n",
        "-framerate",
        str(fps),
        "-i",
        frame_pattern,
    ]

    if audio_path and audio_path.is_file():
        cmd.extend(["-i", str(audio_path.resolve())])

    cmd.extend([
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        "-preset",
        "fast",
        "-movflags",
        "+faststart",
    ])

    if total_frames is not None and total_frames > 0:
        cmd.extend(["-vframes", str(total_frames)])

    if audio_path and audio_path.is_file():
        cmd.extend([
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
        ])

    cmd.append(str(output_mp4.resolve()))

    res = run_hidden_subprocess(cmd)
    if res.returncode != 0:
        raise RenderError(f"FFmpeg encoding failed (exit code {res.returncode}):\n{res.stderr}")

    return output_mp4


def probe_video_metadata(video_path: Path) -> dict[str, Any]:
    """
    Probes video file with ffprobe to verify duration, fps, dimensions, and streams.
    """
    require_tool("ffprobe")
    if not video_path.is_file():
        raise ValidationError(f"Video file not found for probing: {video_path}")

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,duration,nb_frames",
        "-of",
        "json",
        str(video_path.resolve()),
    ]

    res = run_hidden_subprocess(cmd)
    if res.returncode != 0:
        raise ValidationError(f"ffprobe failed on {video_path}: {res.stderr}")

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON from ffprobe: {exc}")

    streams = data.get("streams", [])
    format_info = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    nb_frames = None
    if video_stream:
        raw_nb = video_stream.get("nb_frames")
        if raw_nb and raw_nb != "N/A":
            try:
                nb_frames = int(raw_nb)
            except (ValueError, TypeError):
                pass

    return {
        "file": str(video_path.resolve()),
        "duration_sec": float(format_info.get("duration", 0.0)),
        "file_size_bytes": int(format_info.get("size", 0)),
        "video_stream": video_stream,
        "audio_stream": audio_stream,
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "width": video_stream.get("width") if video_stream else None,
        "height": video_stream.get("height") if video_stream else None,
        "r_frame_rate": video_stream.get("r_frame_rate") if video_stream else None,
        "nb_frames": nb_frames,
    }


# -----------------------------------------------------------------------------
# Contact Sheet Generator
# -----------------------------------------------------------------------------

def generate_contact_sheet(
    frames_dir: Path,
    output_image: Path,
    cols: int = 3,
    sample_count: int = 9,
    fps: int = 30,
    frame_files: list[Path] | None = None,
    total_frames: int | None = None,
) -> Path:
    """
    Creates an inspectable contact-sheet image from rendered frames using Pillow.
    Adds keyframe thumbnails, timestamps, and metadata header.
    Samples evenly across the complete animation timeline (from t=0 to t=duration).
    When frame_files or total_frames is provided, only samples frames that belong
    to the current render, strictly ignoring any stale or extra frames in frames_dir.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise RenderError("Python 'Pillow' is required for contact sheet generation.")

    if frame_files is not None:
        all_frames = [f for f in frame_files if f.is_file()]
    elif total_frames is not None:
        all_frames = [
            frames_dir / f"frame_{i:06d}.png"
            for i in range(total_frames)
            if (frames_dir / f"frame_{i:06d}.png").is_file()
        ]
    else:
        # Fallback for standalone contact-sheet invocation without specified count:
        # Scan strictly consecutive sequence starting from index 0
        all_frames = []
        i = 0
        while (frames_dir / f"frame_{i:06d}.png").is_file():
            all_frames.append(frames_dir / f"frame_{i:06d}.png")
            i += 1
        if not all_frames:
            all_frames = sorted(frames_dir.glob("frame_*.png"))

    if not all_frames:
        raise ValidationError(f"No valid frame PNGs found in {frames_dir}")

    total = total_frames if (total_frames is not None and total_frames == len(all_frames)) else len(all_frames)
    count = min(sample_count, len(all_frames))

    # Evenly distribute sample points across the complete animation range [0, len(all_frames) - 1]
    if count == 1:
        selected_indices = [0]
    else:
        selected_indices = [int(round(i * (len(all_frames) - 1) / (count - 1))) for i in range(count)]
    selected_files = [all_frames[idx] for idx in selected_indices]

    rows = int(math.ceil(count / cols))
    thumb_w = 480

    with Image.open(selected_files[0]) as first_im:
        orig_w, orig_h = first_im.size
        thumb_h = int(thumb_w * (orig_h / orig_w))

    header_h = 70
    footer_label_h = 28
    pad = 16

    sheet_w = cols * thumb_w + (cols + 1) * pad
    sheet_h = header_h + rows * (thumb_h + footer_label_h) + (rows + 1) * pad

    sheet = Image.new("RGB", (sheet_w, sheet_h), color="#080a0f")
    draw = ImageDraw.Draw(sheet)

    # Header
    title_text = f"HTML-VIDEO EVIDENCE CONTACT SHEET | Frames: {total} | Res: {orig_w}x{orig_h} | FPS: {fps}"
    draw.text((pad, 24), title_text, fill="#00f0ff")
    draw.line([(pad, header_h - 10), (sheet_w - pad, header_h - 10)], fill="#1e293b", width=2)

    # Draw thumbnails
    for idx, (f_idx, f_path) in enumerate(zip(selected_indices, selected_files)):
        r = idx // cols
        c = idx % cols
        x = pad + c * (thumb_w + pad)
        y = header_h + pad + r * (thumb_h + footer_label_h + pad)

        with Image.open(f_path) as im:
            thumb = im.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x, y))

        # Thumbnail border
        draw.rectangle([x, y, x + thumb_w, y + thumb_h], outline="#38bdf8", width=1)

        # Timecode label
        t_sec = f_idx / fps
        label = f"Frame {f_idx:06d} (t={t_sec:.3f}s)"
        draw.text((x + 4, y + thumb_h + 6), label, fill="#94a3b8")

    output_image.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(output_image.resolve()), format="PNG")
    return output_image


# -----------------------------------------------------------------------------
# Manifest & Audit Generation
# -----------------------------------------------------------------------------

def build_manifest(
    composition_path: Path,
    output_mp4: Path,
    render_meta: dict[str, Any],
    probe_meta: dict[str, Any],
    contact_sheet_path: Path,
    audio_path: Path | None = None,
) -> dict[str, Any]:
    runtime_versions = get_runtime_versions(
        browser_bin=render_meta.get("browser_binary"),
        browser_version=render_meta.get("browser_version"),
    )

    manifest = {
        "manifest_version": "1.0",
        "composition": {
            "path": str(composition_path.resolve()),
            "sha256": compute_sha256(composition_path),
        },
        "output_video": {
            "path": str(output_mp4.resolve()),
            "sha256": compute_sha256(output_mp4),
            "size_bytes": output_mp4.stat().st_size if output_mp4.exists() else 0,
            "duration_sec": probe_meta.get("duration_sec"),
            "width": probe_meta.get("width"),
            "height": probe_meta.get("height"),
            "r_frame_rate": probe_meta.get("r_frame_rate"),
        },
        "contact_sheet": {
            "path": str(contact_sheet_path.resolve()),
            "sha256": compute_sha256(contact_sheet_path),
        },
        "audio": None,
        "runtime": {
            "python": runtime_versions["python"],
            "platform": runtime_versions["platform"],
            "playwright": runtime_versions["playwright"],
            "playwright_version": runtime_versions["playwright_version"],
            "browser_binary": runtime_versions["browser_binary"],
            "browser_version": runtime_versions["browser_version"],
            "ffmpeg": runtime_versions["ffmpeg"],
            "ffmpeg_version": runtime_versions["ffmpeg_version"],
            "ffprobe": runtime_versions["ffprobe"],
            "ffprobe_version": runtime_versions["ffprobe_version"],
            "pillow": runtime_versions["pillow"],
            "pillow_version": runtime_versions["pillow_version"],
            "font_hash": render_meta.get("font_hash"),
            "fonts": render_meta.get("fonts_info", []),
        },
        "pinned_assets": render_meta.get("pinned_assets", {}),
        "isolation_audit": {
            "blocked_requests_count": len(render_meta.get("blocked_requests", [])),
            "blocked_requests": render_meta.get("blocked_requests", []),
            "page_errors": render_meta.get("page_errors", []),
            "console_errors": render_meta.get("console_errors", []),
        },
        "determinism": render_meta.get("determinism_metric", {}),
    }

    if audio_path and audio_path.is_file():
        manifest["audio"] = {
            "path": str(audio_path.resolve()),
            "sha256": compute_sha256(audio_path),
        }

    return manifest


# -----------------------------------------------------------------------------
# High-Level Render Workflow
# -----------------------------------------------------------------------------

def render_pipeline(
    composition: Path,
    out_dir: Path,
    out_name: str = "output.mp4",
    aspect: str = "16:9",
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    duration: float | None = None,
    audio: Path | None = None,
    admitted_assets: list[str] | None = None,
    force: bool = False,
    chrome_path: str | None = None,
) -> dict[str, Any]:
    """Complete end-to-end rendering and verification pipeline."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_video_path = validate_safe_path(out_dir / out_name, out_dir, allow_overwrite=force)
    frames_dir = validate_safe_path(out_dir / "frames", out_dir, allow_overwrite=True)
    contact_sheet_path = validate_safe_path(out_dir / "contact_sheet.png", out_dir, allow_overwrite=force)
    manifest_path = validate_safe_path(out_dir / "manifest.json", out_dir, allow_overwrite=force)

    # Validate audio if provided
    if audio:
        validate_wav_audio(audio)

    # 1. Render frames with Playwright
    render_meta = render_html_to_frames(
        composition_path=composition,
        frames_dir=frames_dir,
        aspect=aspect,
        override_width=width,
        override_height=height,
        override_fps=fps,
        override_duration=duration,
        admitted_assets=admitted_assets,
        chrome_path=chrome_path,
    )

    effective_fps = render_meta["fps"]
    effective_total_frames = render_meta["total_frames"]

    # 2. Encode to MP4 with FFmpeg strictly limited to rendered frames
    encode_frames_to_mp4(
        frames_dir=frames_dir,
        output_mp4=out_video_path,
        fps=effective_fps,
        total_frames=effective_total_frames,
        audio_path=audio,
        allow_overwrite=force,
    )

    # 3. Technical validation with ffprobe
    probe_meta = probe_video_metadata(out_video_path)

    # 4. Generate contact sheet strictly from current render's frames
    generate_contact_sheet(
        frames_dir=frames_dir,
        output_image=contact_sheet_path,
        cols=3,
        sample_count=9,
        fps=effective_fps,
        frame_files=[Path(f) for f in render_meta.get("frame_files", [])],
        total_frames=effective_total_frames,
    )

    # 5. Write Manifest
    manifest = build_manifest(
        composition_path=composition,
        output_mp4=out_video_path,
        render_meta=render_meta,
        probe_meta=probe_meta,
        contact_sheet_path=contact_sheet_path,
        audio_path=audio,
    )

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return {
        "status": "success",
        "output_mp4": str(out_video_path),
        "manifest": str(manifest_path),
        "contact_sheet": str(contact_sheet_path),
        "probe": probe_meta,
        "determinism": render_meta.get("determinism_metric"),
    }


# -----------------------------------------------------------------------------
# Selftest Routine
# -----------------------------------------------------------------------------

def find_local_system_font() -> Path | None:
    """Locates an existing standard Windows font file to pin in selftest without redistributing in origin."""
    candidates = [
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
        Path(r"C:\Windows\Fonts\tahoma.ttf"),
        Path(r"C:\Windows\Fonts\consola.ttf"),
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    win_fonts = Path(r"C:\Windows\Fonts")
    if win_fonts.is_dir():
        for f in sorted(win_fonts.glob("*.ttf")):
            if f.is_file():
                return f.resolve()
    return None


def run_selftest(
    output_dir: Path,
    chrome_path: str | None = None,
    force: bool = False,
) -> int:
    """
    Executes automated self-test suite rendering both 16:9 and 9:16 aspect ratios (full 5.0s animation),
    audio-bearing MP4 with ffprobe validation, and cross-browser repeated-frame determinism.
    Retains all fixture evidence under output_dir without deletion.
    Fresh unique retained run subdirs allow repeated checks without overwrite when force=False.
    """
    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    default_composition = skill_root / "compositions" / "typography_chart.html"

    if not default_composition.is_file():
        raise ValidationError(f"Default selftest composition missing: {default_composition}")

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine target root for this execution:
    # When output_dir already contains previous test results and force is False,
    # allocate a fresh unique retained run subdirectory so repeated checks succeed without overwrite.
    target_root = output_dir
    if not force:
        has_existing = (output_dir / "test_16_9").exists() or (output_dir / "test_9_16").exists()
        if has_existing:
            idx = 1
            while (output_dir / f"run_{idx:03d}").exists():
                idx += 1
            target_root = output_dir / f"run_{idx:03d}"
            target_root.mkdir(parents=True, exist_ok=True)

    print(f"[selftest] Beginning html-video self-test suite. Target root: {target_root}")

    # Prepare selftest composition package with pinned local font
    # Privately copies an existing Windows font into the retained test package,
    # declares @font-face and admitted asset bytes, and records the actual binary SHA256
    # without redistributing OS fonts in the repository origin.
    sys_font = find_local_system_font()
    font_rel_asset: str | None = None
    selftest_composition = default_composition

    if sys_font:
        test_comp_dir = target_root / "composition"
        test_comp_dir.mkdir(parents=True, exist_ok=True)
        test_assets_dir = test_comp_dir / "assets"
        test_assets_dir.mkdir(parents=True, exist_ok=True)
        font_dest = test_assets_dir / "selftest_font.ttf"
        if not font_dest.exists() or force:
            shutil.copy2(sys_font, font_dest)
        font_rel_asset = "assets/selftest_font.ttf"

        comp_content = default_composition.read_text(encoding="utf-8")
        font_face_style = f"""
  <style>
    @font-face {{
      font-family: 'PinnedSelftestFont';
      src: url('{font_rel_asset}') format('truetype');
      font-display: block;
    }}
    body {{
      font-family: 'PinnedSelftestFont', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
    }}
  </style>
</head>"""
        comp_content = comp_content.replace("</head>", font_face_style, 1)
        comp_content = comp_content.replace(
            "assets: []",
            f"assets: ['{font_rel_asset}']",
            1
        )
        selftest_composition = test_comp_dir / "selftest_typography.html"
        selftest_composition.write_text(comp_content, encoding="utf-8")

    # Case 1: 16:9 Landscape Render (Full 5.0s animation @ 30fps)
    dir_16_9 = target_root / "test_16_9"
    print("[selftest] Rendering 16:9 landscape test (1920x1080, 5.0s @ 30fps)...")
    res_16_9 = render_pipeline(
        composition=selftest_composition,
        out_dir=dir_16_9,
        out_name="video_16_9.mp4",
        aspect="16:9",
        duration=5.0,
        fps=30,
        force=force,
        chrome_path=chrome_path,
    )
    print(f"[selftest] 16:9 MP4 created: {res_16_9['output_mp4']}")
    probe_16_9 = res_16_9["probe"]
    verify_probed_video(
        probe=probe_16_9,
        expected_w=1920,
        expected_h=1080,
        expected_duration=5.0,
        expected_fps=30,
        expected_nb_frames=150,
    )
    det_16_9 = res_16_9["determinism"]
    if not det_16_9.get("tolerance_passed", False):
        raise ValidationError(f"16:9 determinism check failed: {det_16_9}")
    print(f"[selftest] 16:9 Determinism verified (MAE={det_16_9.get('mae')}).")

    # Verify manifest records pinned font with valid SHA256
    manifest_16_9_path = Path(res_16_9["manifest"])
    if manifest_16_9_path.is_file():
        manifest_16_9 = json.loads(manifest_16_9_path.read_text(encoding="utf-8"))
        if font_rel_asset:
            pinned = manifest_16_9.get("pinned_assets", {})
            if font_rel_asset not in pinned:
                raise ValidationError(f"Pinned font {font_rel_asset} not found in manifest pinned_assets")
            font_sha = pinned[font_rel_asset].get("sha256", "")
            if not font_sha or len(font_sha) != 64:
                raise ValidationError(f"Invalid SHA256 for pinned font: {font_sha}")
            print(f"[selftest] Pinned font verified in manifest: {font_rel_asset} (SHA256: {font_sha})")

    # Case 2: 9:16 Portrait Render (Full 5.0s animation @ 30fps)
    dir_9_16 = target_root / "test_9_16"
    print("[selftest] Rendering 9:16 portrait test (1080x1920, 5.0s @ 30fps)...")
    res_9_16 = render_pipeline(
        composition=selftest_composition,
        out_dir=dir_9_16,
        out_name="video_9_16.mp4",
        aspect="9:16",
        duration=5.0,
        fps=30,
        force=force,
        chrome_path=chrome_path,
    )
    print(f"[selftest] 9:16 MP4 created: {res_9_16['output_mp4']}")
    probe_9_16 = res_9_16["probe"]
    verify_probed_video(
        probe=probe_9_16,
        expected_w=1080,
        expected_h=1920,
        expected_duration=5.0,
        expected_fps=30,
        expected_nb_frames=150,
    )
    det_9_16 = res_9_16["determinism"]
    if not det_9_16.get("tolerance_passed", False):
        raise ValidationError(f"9:16 determinism check failed: {det_9_16}")
    print(f"[selftest] 9:16 Determinism verified (MAE={det_9_16.get('mae')}).")

    # Case 3: Audio-Bearing MP4 with Technical WAV Validation and ffprobe
    dir_audio = target_root / "test_audio"
    print("[selftest] Testing audio-bearing MP4 with synthetic WAV and ffprobe validation...")
    wav_path = dir_audio / "audio_track.wav"
    create_synthetic_wav(wav_path, duration_sec=5.0, sample_rate=48000, channels=2)
    res_audio = render_pipeline(
        composition=selftest_composition,
        out_dir=dir_audio,
        out_name="video_audio.mp4",
        aspect="16:9",
        duration=5.0,
        fps=30,
        audio=wav_path,
        force=force,
        chrome_path=chrome_path,
    )
    probe_audio = res_audio["probe"]
    verify_probed_video(
        probe=probe_audio,
        expected_w=1920,
        expected_h=1080,
        expected_duration=5.0,
        expected_fps=30,
        expected_nb_frames=150,
    )
    if not probe_audio.get("has_audio"):
        raise ValidationError(f"Audio stream missing in audio-bearing MP4: {probe_audio}")
    audio_codec = probe_audio["audio_stream"].get("codec_name")
    if audio_codec not in ("aac", "pcm_s16le"):
        raise ValidationError(f"Unexpected audio codec: {audio_codec}")
    print(f"[selftest] Audio-bearing MP4 verified with ffprobe (codec={audio_codec}).")

    # Case 4: Fresh-Browser Repeated-Frame Determinism Audit
    dir_det = target_root / "determinism_audit"
    dir_det.mkdir(parents=True, exist_ok=True)
    print("[selftest] Auditing fresh-browser repeated-frame determinism (independent browser sessions)...")
    fresh_det = verify_fresh_browser_determinism(
        composition_path=selftest_composition,
        evidence_dir=dir_det,
        aspect="16:9",
        test_t=2.5,
        chrome_path=chrome_path,
    )
    if not fresh_det.get("tolerance_passed", False):
        raise ValidationError(f"Fresh-browser determinism check failed: {fresh_det}")
    print(f"[selftest] Fresh-browser repeated-frame determinism passed (MAE={fresh_det.get('mae')}).")

    print(f"[selftest] All selftest cases passed successfully. All evidence preserved in {target_root}.")
    return 0


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Deterministic HTML-to-Video rendering engine with network isolation."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Render Command
    cmd_render = sub.add_parser("render", help="Render an HTML composition to MP4.")
    cmd_render.add_argument("--composition", required=True, type=Path, help="Path to HTML composition.")
    cmd_render.add_argument(
        "--out-dir",
        "--output-dir",
        "--output-root",
        dest="out_dir",
        required=True,
        type=Path,
        help="Explicit output directory.",
    )
    cmd_render.add_argument("--out-name", default="output.mp4", help="Name of output MP4.")
    cmd_render.add_argument("--aspect", default="16:9", choices=["16:9", "9:16", "1:1"], help="Aspect ratio.")
    cmd_render.add_argument("--width", type=int, help="Override width in pixels.")
    cmd_render.add_argument("--height", type=int, help="Override height in pixels.")
    cmd_render.add_argument("--fps", type=int, help="Override frame rate.")
    cmd_render.add_argument("--duration", type=float, help="Override duration in seconds.")
    cmd_render.add_argument("--audio", type=Path, help="Optional path to local WAV audio file.")
    cmd_render.add_argument("--admit-asset", action="append", default=[], help="Explicitly admit relative asset path.")
    cmd_render.add_argument("--force", action="store_true", default=False, help="Allow overwrite of existing output files.")
    cmd_render.add_argument("--chrome-path", help="Explicit path to Chrome executable.")

    # Probe Command
    cmd_probe = sub.add_parser("probe", help="Inspect video metadata with ffprobe.")
    cmd_probe.add_argument("--input", required=True, type=Path, help="Path to MP4 video.")

    # Contact Sheet Command
    cmd_sheet = sub.add_parser("contact-sheet", help="Generate contact sheet from PNG frame sequence.")
    cmd_sheet.add_argument("--frames-dir", required=True, type=Path, help="Directory containing frame_*.png.")
    cmd_sheet.add_argument("--out", required=True, type=Path, help="Output PNG path.")
    cmd_sheet.add_argument("--cols", type=int, default=3, help="Grid columns.")
    cmd_sheet.add_argument("--count", type=int, default=9, help="Number of sample frames.")
    cmd_sheet.add_argument("--fps", type=int, default=30, help="Framerate for timecode labels.")
    cmd_sheet.add_argument("--total-frames", type=int, help="Exact number of frames belonging to video.")

    # Selftest Command
    cmd_selftest = sub.add_parser("selftest", help="Run end-to-end self-test suite.")
    cmd_selftest.add_argument(
        "--output-dir",
        "--output-root",
        dest="output_dir",
        required=True,
        type=Path,
        help="Explicit output directory for retained test evidence.",
    )
    cmd_selftest.add_argument("--chrome-path", help="Explicit path to Chrome executable.")
    cmd_selftest.add_argument("--force", action="store_true", default=False, help="Allow overwrite of existing output files.")

    return ap


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.cmd == "render":
            res = render_pipeline(
                composition=args.composition,
                out_dir=args.out_dir,
                out_name=args.out_name,
                aspect=args.aspect,
                width=args.width,
                height=args.height,
                fps=args.fps,
                duration=args.duration,
                audio=args.audio,
                admitted_assets=args.admit_asset,
                force=args.force,
                chrome_path=args.chrome_path,
            )
            print(json.dumps(res, indent=2))
            return 0

        elif args.cmd == "probe":
            meta = probe_video_metadata(args.input)
            print(json.dumps(meta, indent=2))
            return 0

        elif args.cmd == "contact-sheet":
            out_img = generate_contact_sheet(
                frames_dir=args.frames_dir,
                output_image=args.out,
                cols=args.cols,
                sample_count=args.count,
                fps=args.fps,
                total_frames=getattr(args, "total_frames", None),
            )
            print(f"contact_sheet={out_img.resolve()}")
            return 0

        elif args.cmd == "selftest":
            return run_selftest(
                output_dir=args.output_dir,
                chrome_path=args.chrome_path,
                force=args.force,
            )

    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("aborted by user", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"unexpected exception: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
