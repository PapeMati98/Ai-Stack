"""Bounded no-delete download finalization (GUID.tmp -> target filename) for Fleet Chrome Relay.

Addresses HD-20261014-b53dfc: when Chrome's extension waitForEvent('download') times out
after a UI Blob/ZIP/XLSX export while leaving a complete valid payload on disk as
`<GUID>.tmp`, this module verifies file stability and ZIP/XLSX CRC integrity and
finalizes the artifact into the expected target filename without deleting `<GUID>.tmp`
or overwriting unrelated files.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Callable

FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,199}\Z")
GUID_TMP_RE = re.compile(
    r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[A-Za-z0-9._-]{4,128})\.tmp\Z"
)
ZIP_EXTENSIONS = (".zip", ".xlsx", ".xlsm", ".docx", ".pptx")


def _safe_dir(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError("invalid_directory")
    text = str(value)
    if not text or "\x00" in text or ".." in Path(text).parts:
        raise ValueError("invalid_directory")
    path = Path(text).resolve()
    if not path.is_dir():
        raise ValueError("directory_not_found")
    return path


def _safe_filename(name: str) -> str:
    if not isinstance(name, str) or "\x00" in name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("invalid_filename")
    if not FILENAME_RE.fullmatch(name):
        raise ValueError("invalid_filename")
    lower = name.lower()
    if lower.endswith((".tmp", ".crdownload", ".part")):
        raise ValueError("invalid_filename")
    return name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_zip_integrity(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 22:
            return False
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path, "r") as archive:
            if not archive.namelist():
                return False
            return archive.testzip() is None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


def _is_stable_file(path: Path, sample_delay_s: float) -> tuple[bool, int]:
    try:
        st1 = path.stat()
        if st1.st_size <= 0:
            return False, 0
        if sample_delay_s > 0:
            time.sleep(sample_delay_s)
        st2 = path.stat()
        if st1.st_size != st2.st_size or st1.st_mtime_ns != st2.st_mtime_ns:
            return False, 0
        return True, st2.st_size
    except OSError:
        return False, 0


def _copy_no_delete(source: Path, destination: Path) -> str:
    source_sha = sha256_file(source)
    if destination.exists():
        if destination.is_file() and sha256_file(destination) == source_sha:
            return source_sha
        raise FileExistsError("target_exists_mismatch")
    shutil.copy2(source, destination)
    dest_sha = sha256_file(destination)
    if dest_sha != source_sha:
        raise OSError("copy_verification_failed")
    return dest_sha


def finalize_download(
    download_dir: str | Path,
    expected_filename: str,
    *,
    started_after_ns: int | None = None,
    timeout_s: float = 15.0,
    poll_interval_s: float = 0.05,
    require_zip: bool | None = None,
    output_dir: str | Path | None = None,
) -> dict:
    """Resolve either the named download or a finalized Chrome `<GUID>.tmp` into `expected_filename`.

    Preserves `<GUID>.tmp` (no-delete policy) and never overwrites an existing file with different bytes.
    """
    try:
        src_dir = _safe_dir(download_dir)
        dst_dir = _safe_dir(output_dir) if output_dir is not None else src_dir
        clean_name = _safe_filename(expected_filename)
    except ValueError as exc:
        return {"status": "denied", "data": {"reason": str(exc)}}

    if not isinstance(timeout_s, (int, float)) or not 0.0 <= float(timeout_s) <= 300.0:
        return {"status": "denied", "data": {"reason": "bound"}}
    if not isinstance(poll_interval_s, (int, float)) or not 0.0 <= float(poll_interval_s) <= 10.0:
        return {"status": "denied", "data": {"reason": "bound"}}

    check_zip = clean_name.lower().endswith(ZIP_EXTENSIONS) if require_zip is None else bool(require_zip)
    deadline = time.monotonic() + float(timeout_s)
    sample_delay = min(float(poll_interval_s), 0.05)
    target_path = dst_dir / clean_name
    named_in_src = src_dir / clean_name

    last_reason = "timeout"
    while True:
        # 1. Check if Chrome already finalized the named file in download_dir.
        if named_in_src.is_file():
            try:
                st = named_in_src.stat()
            except OSError:
                st = None
            if st is not None and (started_after_ns is None or st.st_mtime_ns >= started_after_ns):
                stable, size_bytes = _is_stable_file(named_in_src, sample_delay)
                if stable:
                    if check_zip and not verify_zip_integrity(named_in_src):
                        last_reason = "corrupt_zip_payload"
                    else:
                        try:
                            digest = _copy_no_delete(named_in_src, target_path)
                        except FileExistsError:
                            return {"status": "denied", "data": {"reason": "target_exists_mismatch"}}
                        except OSError:
                            return {"status": "denied", "data": {"reason": "io_error"}}
                        return {
                            "status": "ok",
                            "data": {
                                "source_kind": "named",
                                "source_path": str(named_in_src),
                                "final_path": str(target_path),
                                "filename": clean_name,
                                "size_bytes": size_bytes,
                                "sha256": digest,
                                "zip_verified": check_zip,
                            },
                        }

        # 2. Check if an active .crdownload is still in progress.
        active_crdownload = False
        try:
            entries = list(src_dir.iterdir())
        except OSError:
            return {"status": "denied", "data": {"reason": "io_error"}}

        for entry in entries:
            if entry.name.lower().endswith(".crdownload") and entry.is_file():
                try:
                    st = entry.stat()
                    if started_after_ns is None or st.st_mtime_ns >= started_after_ns:
                        active_crdownload = True
                        break
                except OSError:
                    continue

        if not active_crdownload:
            candidates: list[tuple[int, Path, int, str]] = []
            saw_corrupt_tmp = False
            for entry in entries:
                if not GUID_TMP_RE.fullmatch(entry.name) or not entry.is_file():
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if started_after_ns is not None and st.st_mtime_ns < started_after_ns:
                    continue
                stable, size_bytes = _is_stable_file(entry, sample_delay)
                if not stable:
                    continue
                if check_zip and not verify_zip_integrity(entry):
                    saw_corrupt_tmp = True
                    continue
                try:
                    digest = sha256_file(entry)
                except OSError:
                    continue
                candidates.append((st.st_mtime_ns, entry, size_bytes, digest))

            if candidates:
                distinct_hashes = {item[3] for item in candidates}
                if len(distinct_hashes) > 1:
                    return {"status": "denied", "data": {"reason": "ambiguous_tmp_candidates"}}
                candidates.sort(key=lambda item: item[0], reverse=True)
                _, chosen_path, size_bytes, _ = candidates[0]
                try:
                    digest = _copy_no_delete(chosen_path, target_path)
                except FileExistsError:
                    return {"status": "denied", "data": {"reason": "target_exists_mismatch"}}
                except OSError:
                    return {"status": "denied", "data": {"reason": "io_error"}}
                return {
                    "status": "ok",
                    "data": {
                        "source_kind": "guid_tmp",
                        "source_path": str(chosen_path),
                        "final_path": str(target_path),
                        "filename": clean_name,
                        "size_bytes": size_bytes,
                        "sha256": digest,
                        "zip_verified": check_zip,
                        "guid_tmp_preserved": chosen_path.is_file(),
                    },
                }
            if saw_corrupt_tmp:
                last_reason = "corrupt_zip_payload"

        if time.monotonic() >= deadline:
            return {"status": "denied", "data": {"reason": last_reason}}
        if poll_interval_s > 0:
            time.sleep(min(float(poll_interval_s), max(0.0, deadline - time.monotonic())))


def wait_for_download_or_finalize(
    wait_event_fn: Callable[[], dict | str | Path | None],
    download_dir: str | Path,
    expected_filename: str,
    **kwargs,
) -> dict:
    """Execute `wait_event_fn` (e.g. extension `waitForEvent('download')`); on timeout or `.tmp` path, finalize."""
    event_error = None
    event_path = None
    try:
        raw = wait_event_fn()
        if isinstance(raw, (str, Path)):
            event_path = str(raw)
        elif isinstance(raw, dict) and isinstance(raw.get("path"), str):
            event_path = raw["path"]
    except TimeoutError:
        event_error = "waitForEvent_timeout"
    except Exception as exc:
        event_error = type(exc).__name__

    result = finalize_download(download_dir, expected_filename, **kwargs)
    if result.get("status") == "ok":
        result["data"]["wait_event_status"] = "timeout_recovered" if event_error else "completed"
        if event_path:
            result["data"]["reported_event_path"] = event_path
    return result


def build_synthetic_zip_bytes(marker: str = "synthetic-b53dfc") -> bytes:
    """Create a valid in-memory ZIP/XLSX-structured archive with deterministic synthetic content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types/>')
        zf.writestr("xl/workbook.xml", f'<workbook marker="{marker}"/>')
        zf.writestr("data/export.json", f'{{"ticket":"HD-20261014-b53dfc","marker":"{marker}"}}')
    return buf.getvalue()


def run_synthetic_blob_zip_reproducer(work_dir: str | Path) -> dict:
    """Reproduce the HD-20261014-b53dfc scenario in an isolated working directory and verify finalization.

    1. Emits a valid ZIP/XLSX payload as `<GUID>.tmp` inside `work_dir/downloads`.
    2. Simulates `waitForEvent('download')` timing out without emitting the final named-path event.
    3. Finalizes `<GUID>.tmp` -> `synthetic-export.xlsx` via `wait_for_download_or_finalize`.
    4. Verifies `<GUID>.tmp` is preserved (no-delete), `synthetic-export.xlsx` exists, SHA256 matches, and ZIP CRC is valid.
    """
    base = _safe_dir(work_dir)
    dl_dir = base / "downloads"
    out_dir = base / "outputs"
    dl_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    started_ns = time.time_ns()
    payload = build_synthetic_zip_bytes("HD-20261014-b53dfc-canary")
    expected_sha = hashlib.sha256(payload).hexdigest()
    guid_name = f"{uuid.uuid4()}.tmp"
    guid_path = dl_dir / guid_name
    guid_path.write_bytes(payload)

    def timed_out_wait_for_event():
        raise TimeoutError("waitForEvent(download) timed out after UI export")

    outcome = wait_for_download_or_finalize(
        timed_out_wait_for_event,
        dl_dir,
        "synthetic-export.xlsx",
        started_after_ns=started_ns - 1_000_000_000,
        timeout_s=2.0,
        poll_interval_s=0.02,
        output_dir=out_dir,
    )
    final_path = out_dir / "synthetic-export.xlsx"
    ok = (
        outcome.get("status") == "ok"
        and outcome.get("data", {}).get("source_kind") == "guid_tmp"
        and outcome.get("data", {}).get("wait_event_status") == "timeout_recovered"
        and outcome.get("data", {}).get("sha256") == expected_sha
        and guid_path.is_file()
        and final_path.is_file()
        and verify_zip_integrity(final_path)
    )
    return {
        "ok": ok,
        "expected_sha256": expected_sha,
        "guid_tmp_path": str(guid_path),
        "guid_tmp_preserved": guid_path.is_file(),
        "final_path": str(final_path),
        "final_exists": final_path.is_file(),
        "zip_crc_ok": verify_zip_integrity(final_path) if final_path.is_file() else False,
        "outcome": outcome,
    }


def run_chromium_blob_zip_canary(work_dir: str | Path) -> dict:
    """Run a real headless Chromium Blob(ZIP/XLSX) UI export and finalize GUID.tmp -> target name."""
    import base64
    import json
    import subprocess
    import sys

    import playwright

    base = _safe_dir(work_dir)
    dl_dir = base / "chromium-downloads"
    out_dir = base / "chromium-outputs"
    dl_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    pkg_root = Path(playwright.__file__).resolve().parent / "driver" / "package"
    node_exe = pkg_root.parent / "node.exe"
    payload = build_synthetic_zip_bytes("chromium-blob-b53dfc")
    expected_sha = hashlib.sha256(payload).hexdigest()
    b64_payload = base64.b64encode(payload).decode("ascii")
    guid_name = f"{uuid.uuid4()}.tmp"
    guid_path = dl_dir / guid_name

    cjs_path = base / "chromium_blob_export.cjs"
    cjs_code = f"""'use strict';
const fs = require('fs');
const {{ chromium }} = require({json.dumps(str(pkg_root))});
const B64 = {json.dumps(b64_payload)};
const GUID_PATH = {json.dumps(str(guid_path))};
const URL_MAIN = 'http://127.0.0.1:8124/export';
const HTML = '<!doctype html><html><body>' +
  '<button id="export-btn">Export XLSX</button>' +
  '<script>' +
  'const raw = atob("' + B64 + '");' +
  'const bytes = new Uint8Array(raw.length);' +
  'for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);' +
  'document.getElementById("export-btn").addEventListener("click", () => {{' +
  '  const blob = new Blob([bytes], {{ type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }});' +
  '  window.__blobSize = blob.size;' +
  '  const url = URL.createObjectURL(blob);' +
  '  const a = document.createElement("a");' +
  '  a.href = url; a.download = "browser-export.xlsx";' +
  '  document.body.appendChild(a); a.click();' +
  '}});' +
  '</script></body></html>';

(async () => {{
  const browser = await chromium.launch({{
    headless: true,
    args: ['--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'],
  }});
  try {{
    const context = await browser.newContext({{ acceptDownloads: true }});
    await context.route('**/*', async (route) => {{
      if (route.request().url() === URL_MAIN) {{
        await route.fulfill({{ status: 200, contentType: 'text/html', body: HTML }});
      }} else {{
        await route.abort();
      }}
    }});
    const page = await context.newPage();
    await page.goto(URL_MAIN, {{ waitUntil: 'load' }});
    const [download] = await Promise.all([
      page.waitForEvent('download', {{ timeout: 10000 }}),
      page.click('#export-btn'),
    ]);
    // Save the real Chromium-downloaded Blob stream as GUID.tmp to reproduce Chrome leaving GUID.tmp
    await download.saveAs(GUID_PATH);
    const blobSize = await page.evaluate(() => window.__blobSize || 0);
    console.log(JSON.stringify({{ ok: true, suggestedFilename: download.suggestedFilename(), blobSize }}));
    await context.close();
  }} finally {{
    await browser.close();
  }}
}})();
"""
    cjs_path.write_text(cjs_code, encoding="utf-8")
    started_ns = time.time_ns() - 1_000_000_000
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    proc = subprocess.run(
        [str(node_exe), str(cjs_path)],
        cwd=str(base),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        **kwargs,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr or proc.stdout}
    browser_meta = json.loads(proc.stdout.strip().splitlines()[-1])

    def simulated_extension_wait_timeout():
        raise TimeoutError("extension waitForEvent(download) timed out while GUID.tmp is complete on disk")

    outcome = wait_for_download_or_finalize(
        simulated_extension_wait_timeout,
        dl_dir,
        browser_meta.get("suggestedFilename") or "browser-export.xlsx",
        started_after_ns=started_ns,
        timeout_s=3.0,
        poll_interval_s=0.02,
        output_dir=out_dir,
    )
    final_path = out_dir / "browser-export.xlsx"
    ok = (
        outcome.get("status") == "ok"
        and outcome.get("data", {}).get("source_kind") == "guid_tmp"
        and outcome.get("data", {}).get("sha256") == expected_sha
        and guid_path.is_file()
        and final_path.is_file()
        and verify_zip_integrity(final_path)
    )
    return {
        "ok": ok,
        "browser_meta": browser_meta,
        "expected_sha256": expected_sha,
        "guid_tmp_path": str(guid_path),
        "guid_tmp_preserved": guid_path.is_file(),
        "final_path": str(final_path),
        "zip_crc_ok": verify_zip_integrity(final_path) if final_path.is_file() else False,
        "outcome": outcome,
    }

