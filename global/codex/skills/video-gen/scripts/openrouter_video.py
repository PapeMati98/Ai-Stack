#!/usr/bin/env python3
"""Minimal OpenRouter video CLI.

Uses:
  - GET  /api/v1/videos/models
  - POST /api/v1/videos
  - GET  polling_url or /api/v1/videos/{id}
  - GET  /api/v1/videos/{id}/content or unsigned content URL
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = "https://openrouter.ai/api/v1"


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the API key when a redirect leaves the original origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError(f"Refusing non-HTTPS redirect: {newurl}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl)[:2] != urllib.parse.urlsplit(req.full_url)[:2]:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_SameOriginRedirect)


def _is_api_url(url: str) -> bool:
    """The API key is only ever sent to the OpenRouter API itself."""
    return url.startswith(BASE_URL + "/")


def _require_https(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise SystemExit(f"Refusing non-HTTPS download URL: {url}")


def _windows_env(name: str) -> tuple[str, str] | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None

    keys = [
        ("Windows User environment", winreg.HKEY_CURRENT_USER, "Environment"),
        (
            "Windows Machine environment",
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ]
    for source, hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        value = str(value).strip()
        if value:
            return value, source
    return None


def _key_with_source() -> tuple[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key, "process environment"

    persisted = _windows_env("OPENROUTER_API_KEY")
    if persisted:
        return persisted

    raise SystemExit(
        "OPENROUTER_API_KEY is not available. Set it in the process environment "
        "or as a persistent Windows User/Machine environment variable; do not paste keys into chat."
    )


def _key() -> str:
    return _key_with_source()[0]


def _headers(auth: bool = True) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": "https://codex.local",
        "X-Title": "Codex Video Gen Skill",
    }
    if auth:
        headers["Authorization"] = f"Bearer {_key()}"
    return headers


def _request_json(method: str, url: str, payload: dict | None = None, auth: bool = True) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(auth=auth and _is_api_url(url)), method=method)
    try:
        with _OPENER.open(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"OpenRouter HTTP {exc.code}: {body}") from exc


def _download(url: str, out: Path) -> None:
    _require_https(url)
    req = urllib.request.Request(url, headers=_headers(auth=_is_api_url(url)), method="GET")
    try:
        with _OPENER.open(req, timeout=180) as resp:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Download HTTP {exc.code}: {body}") from exc


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def list_models(_: argparse.Namespace) -> int:
    data = _request_json("GET", f"{BASE_URL}/videos/models", auth=False)
    rows = data.get("data", data if isinstance(data, list) else [])
    for model in rows:
        mid = model.get("id", "")
        name = model.get("name", "")
        resolutions = ",".join(model.get("supported_resolutions") or [])
        durations = model.get("supported_durations") or []
        dur = f"{min(durations)}-{max(durations)}s" if durations else ""
        frames = ",".join(model.get("supported_frame_images") or [])
        print(f"{mid}\t{name}\t{resolutions}\t{dur}\tframes:{frames}")
    return 0


def check_key(_: argparse.Namespace) -> int:
    key, source = _key_with_source()
    print(f"OPENROUTER_API_KEY found in {source}; length={len(key)}; value=hidden")
    return 0


def generate(args: argparse.Namespace) -> int:
    payload: dict = {
        "model": args.model,
        "prompt": args.prompt,
    }
    if args.duration:
        payload["duration"] = args.duration
    if args.size:
        payload["size"] = args.size
    if args.resolution:
        payload["resolution"] = args.resolution
    if args.aspect_ratio:
        payload["aspect_ratio"] = args.aspect_ratio
    if args.seed is not None:
        payload["seed"] = args.seed

    if args.first_frame:
        first = Path(args.first_frame)
        if not first.exists():
            raise SystemExit(f"first-frame file not found: {first}")
        payload["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": _data_url(first)},
                "frame_type": "first_frame",
            }
        ]

    if args.last_frame:
        last = Path(args.last_frame)
        if not last.exists():
            raise SystemExit(f"last-frame file not found: {last}")
        payload.setdefault("frame_images", []).append(
            {
                "type": "image_url",
                "image_url": {"url": _data_url(last)},
                "frame_type": "last_frame",
            }
        )

    if args.extra_json:
        try:
            extra = json.loads(args.extra_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--extra-json is not valid JSON: {exc}") from exc
        if not isinstance(extra, dict):
            raise SystemExit("--extra-json must decode to an object")
        payload.update(extra)

    submitted = _request_json("POST", f"{BASE_URL}/videos", payload=payload)
    job_id = submitted.get("id")
    polling_url = submitted.get("polling_url") or (f"{BASE_URL}/videos/{job_id}" if job_id else None)
    if not job_id or not polling_url:
        print(json.dumps(submitted, indent=2, ensure_ascii=False))
        raise SystemExit("OpenRouter response did not include id/polling_url")

    print(f"job_id={job_id}")
    print(f"status={submitted.get('status', 'submitted')}")

    status = submitted
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(args.poll_interval)
        _require_https(polling_url)
        status = _request_json("GET", polling_url)
        state = status.get("status", "unknown")
        print(f"status={state}")
        if state in {"completed", "succeeded", "success"}:
            break
        if state in {"failed", "cancelled", "canceled", "error"}:
            print(json.dumps(status, indent=2, ensure_ascii=False))
            raise SystemExit(f"Video generation failed: {state}")
    else:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        raise SystemExit("Timed out waiting for video generation")

    content_url = None
    unsigned = status.get("unsigned_urls") or status.get("urls") or []
    if unsigned:
        content_url = unsigned[0]
    if not content_url:
        content_url = f"{BASE_URL}/videos/{job_id}/content"

    out = Path(args.out)
    _download(content_url, out)
    print(f"saved={out.resolve()}")

    if args.meta_out:
        meta = Path(args.meta_out)
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"request": payload, "submit": submitted, "final": status}, indent=2), encoding="utf-8")
        print(f"metadata={meta.resolve()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate videos through OpenRouter.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lm = sub.add_parser("list-models", help="List OpenRouter video models.")
    lm.set_defaults(func=list_models)

    ck = sub.add_parser("check-key", help="Check whether OPENROUTER_API_KEY is available without printing it.")
    ck.set_defaults(func=check_key)

    gen = sub.add_parser("generate", help="Submit, poll, and download a video generation job.")
    gen.add_argument("--model", default="alibaba/happyhorse-1.1")
    gen.add_argument("--prompt", required=True)
    gen.add_argument("--duration", type=int, default=5)
    gen.add_argument("--size", default="1280x720")
    gen.add_argument("--resolution")
    gen.add_argument("--aspect-ratio")
    gen.add_argument("--seed", type=int)
    gen.add_argument("--first-frame")
    gen.add_argument("--last-frame")
    gen.add_argument("--extra-json", help="JSON object with provider-specific passthrough parameters.")
    gen.add_argument("--out", required=True)
    gen.add_argument("--meta-out")
    gen.add_argument("--poll-interval", type=int, default=15)
    gen.add_argument("--timeout", type=int, default=1800)
    gen.set_defaults(func=generate)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
