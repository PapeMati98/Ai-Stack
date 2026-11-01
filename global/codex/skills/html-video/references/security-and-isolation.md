# Security and Isolation Architecture for html-video

This document defines the sandboxing, process execution, and isolation requirements for the `html-video` renderer.

## 1. Network & Asset Boundary

The renderer runs HTML compositions inside a strictly isolated headless browser context:

1. **Route Interception & WebSocket Isolation**:
   - `context.route('**/*', handler)` intercepts all network requests at the BrowserContext level. Registration must succeed before navigation occurs; failing closed immediately with `SecurityError` if unavailable or if registration raises. Context isolation is never downgraded to page-only routing.
   - Any URL with scheme `http:`, `https:`, `ws:`, `wss:`, or `ftp:` is immediately aborted with `blockedbyclient`.
   - Pre-network WebSocket routing via `context.route_web_socket('**/*', handler)` intercepts connection attempts before any network packets are transmitted, closing `WebSocketRoute` immediately with code 1008 without ever invoking `connect_to_server()`. Registration is strictly required and fails closed if unavailable or if registration raises.
   - WebSockets, Service Workers, and Shared Workers are additionally suppressed via:
     - Chromium feature flag `--disable-features=ServiceWorker,SharedWorker`.
     - Playwright context configuration `service_workers="block"`.
     - In-page script overriding `window.WebSocket`, `window.SharedWorker`, and `navigator.serviceWorker`.
     - Playwright `page.on("websocket")` and `context.on("websocket")` event listeners closing any connection attempts.
2. **Local File Access Restriction**:
   - Only `file://` URLs pointing to the root composition file or explicitly admitted and pinned relative asset paths inside the composition folder are allowed.
   - Any attempt to load arbitrary system files (e.g. `file:///C:/Windows/`, `file:///C:/Users/`) or unpinned local files in the composition directory is aborted with `accessdenied`.
   - Inlined `data:` URIs (e.g. `data:image/svg+xml;...`) are permitted if they do not reference external resources.
3. **Asset Pinning & Readiness**:
   - Assets declared in the composition (or via `--admit-asset`) are checked for existence, resolved strictly within the composition directory, and have their SHA-256 hashes recorded in `manifest.json`.
   - Compositions declare local font files in `renderTimeline.assets`, and their binary file SHA-256 hashes are pinned and verified in `manifest.json`. Selftest privately copies an existing local Windows font into its retained output package, declares `@font-face` and admitted asset bytes, and proves the actual binary SHA-256 without redistributing OS fonts in the skill origin.
   - Before capturing frames, the engine waits for `document.readyState === 'complete'`, `document.fonts.ready`, image element completion (`naturalWidth > 0`), timeline hook readiness (`window.seek`), and explicit lifecycle marker (`data-timeline-ready === 'true'`). Missing assets or readiness timeout trigger an immediate abort.

## 2. Headless Browser Hardening

1. **Chrome Binary Location**:
   - Primary: `C:/Program Files/Google/Chrome/Application/chrome.exe`.
   - Secondary: `C:/Program Files (x86)/Google/Chrome/Application/chrome.exe` or `CHROME_BIN` environment variable.
2. **Honest Browser Boundary & Sandboxing**:
   - The browser is launched with native Chromium OS sandboxing explicitly enabled via `chromium_sandbox=True` in Playwright `p.chromium.launch(...)`. The insecure `--no-sandbox` flag is strictly omitted.
   - Process-level OS sandbox boundaries isolate the browser renderer from the host operating system, while Playwright context routing strictly restricts network protocols and filesystem file access.
3. **Fresh Incognito Profile**:
   - Every rendering session creates a brand new temporary browser context. No shared cookies, cache, local storage, or history.
4. **Hardened Launch Arguments**:
   - `--disable-features=ServiceWorker,SharedWorker`
   - `--disable-extensions`
   - `--disable-background-networking`
   - `--disable-default-apps`
   - `--disable-sync`
   - `--disable-translate`
   - `--metrics-recording-only`
   - `--no-first-run`
   - `--headless=new`
   - `--mute-audio`

## 3. Subprocess Safety on Windows

1. **Hidden Console Windows**:
   - The workstation policy forbids unsolicited visible console windows during automated agent runs.
   - All subprocess calls (FFmpeg, ffprobe, Chrome) set `creationflags=subprocess.CREATE_NO_WINDOW` (`0x08000000`) on `sys.platform == 'win32'`.
   - `STARTUPINFO` with `STARTF_USESHOWWINDOW` and `wShowWindow = 0` is used.
2. **No Shell Execution**:
   - Never use `shell=True` in `subprocess.run` or `subprocess.Popen`.
   - Command arguments are strictly passed as an array of strings (`list[str]`) to prevent shell injection.
3. **Bytecode Suppression**:
   - Python processes operate with `sys.dont_write_bytecode = True` and `PYTHONDONTWRITEBYTECODE=1` to prevent `.pyc` clutter in worktrees.

## 4. Path Validation & Output Boundaries

1. **Destination Boundary**:
   - The caller must explicitly declare an output directory (`--out-dir` or `--output-dir`).
   - Every output file and directory (MP4 video, PNG frames directory, contact sheet, manifest) is resolved and verified with `.relative_to(resolved_out_dir)` via `validate_safe_path`.
   - Paths escaping the boundary via `..` or alternate drive letters are rejected with a `SecurityError`.
2. **Refusal to Overwrite**:
   - If an output MP4 or key output file already exists, the renderer aborts immediately unless `--force` is explicitly provided.
   - For `selftest`, `force` defaults to `False`. When output already exists, selftest allocates a fresh unique retained run directory (e.g. `run_001/`, `run_002/`) so repeated verification runs succeed without overwrite conflicts.
3. **Evidence Retention**:
   - Test runs and selftests write artifacts to designated fixture/evidence folders (e.g. `<owned-evidence-dir>`).
   - Automated scripts must never perform destructive `rmdir`, `unlink`, or temporary directory cleanup; evidence is retained for independent inspection.

## 5. Audio Technical Validation

If an external audio track is provided:
- Must be a local uncompressed WAV file (`.wav`).
- Must pass header verification (RIFF chunk, WAVE format, valid PCM audio format code `0x0001`, 1 or 2 channels, standard sample rate 44.1kHz, 48kHz, or 96kHz, valid bit depth).
- Refuses corrupt, truncated, or non-WAV inputs before passing to FFmpeg.
