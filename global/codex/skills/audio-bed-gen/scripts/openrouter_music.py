#!/usr/bin/env python3
"""OpenRouter music generation helper for audio-bed-gen.

Uses OpenRouter's OpenAI-compatible chat completions endpoint for audio output.
The script deliberately supports multiple response shapes and writes metadata for
debugging without logging secrets.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


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
        raise MusicGenError(f"Refusing non-HTTPS download URL: {url}")
DEFAULT_MODEL = "google/lyria-3-pro-preview"
FALLBACK_MODEL = "google/lyria-3-clip-preview"
VET_EYE_PROMPT = """Create an original instrumental background music bed for a polished student executive presentation and product demo.

The video presents Vet-Eye Smart Imaging, an AI-supported veterinary ultrasound imaging feature. The narrator is DOG TOR, a friendly but professional dog doctor avatar. The topic is serious and business-oriented: reducing manual ultrasound image adjustments, supporting clinicians, improving clinic workflow, and presenting a controlled AI implementation plan.

Mood:
professional, clean, warm, trustworthy, intelligent, calm, slightly optimistic.

Style:
modern medical technology, subtle corporate innovation, light AI product demo, executive presentation.

Instrumentation:
soft synth pads, gentle piano or felt piano, subtle pulse, light electronic texture, warm low synth, restrained percussion, minimal melodic motif.

Tempo:
85-92 BPM.

Structure:
0-8 seconds: soft clean intro.
8-90 seconds: stable subtle motion under narration.
90-115 seconds: slightly more confident and optimistic ending.
Final 2 seconds: clean fade-out.

Restrictions:
Instrumental only. No vocals. No lyrics. No choir. No spoken words. No famous artist imitation. No copyrighted-song imitation. No cinematic trailer drums. No comedy. No childish pet-video tone. No dramatic hospital drama. No horror. No distracting lead melody. The music must leave space for Polish voice-over narration.

Target duration:
115 seconds.

Output:
High-quality stereo background music suitable for mixing under voice-over.
"""


class MusicGenError(RuntimeError):
    pass


def _windows_env(name: str) -> tuple[str, str] | None:
    if os.name != "nt":
        return None
    try:
        import winreg  # type: ignore
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
    raise MusicGenError(
        "OPENROUTER_API_KEY is not available. Set it in the process environment "
        "or as a persistent Windows User/Machine environment variable; do not paste keys into chat."
    )


def _key() -> str:
    return _key_with_source()[0]


def _headers(auth: bool = True, accept: str = "application/json") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": accept,
        "HTTP-Referer": "https://codex.local",
        "X-Title": "Codex Audio Bed Gen Skill",
    }
    if auth:
        headers["Authorization"] = f"Bearer {_key()}"
    return headers


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None, auth: bool = True, timeout: int = 120) -> dict[str, Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(auth=auth and _is_api_url(url)), method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MusicGenError(f"OpenRouter HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise MusicGenError(f"OpenRouter request failed: {exc}") from exc


def _stream_chat_completion(payload: dict[str, Any], timeout: int) -> tuple[bytes | None, dict[str, Any]]:
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_headers(auth=True, accept="text/event-stream"),
        method="POST",
    )
    chunks: list[str] = []
    transcript_parts: list[str] = []
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    final_json: dict[str, Any] | None = None

    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    events.append({"raw": data})
                    continue
                events.append(event)
                final_json = event
                choices = event.get("choices") or []
                for choice in choices:
                    delta = choice.get("delta") or {}
                    message = choice.get("message") or {}
                    for source in (delta, message):
                        if isinstance(source.get("content"), str):
                            text_parts.append(source["content"])
                        audio = source.get("audio")
                        if isinstance(audio, dict):
                            if audio.get("data"):
                                chunks.append(audio["data"])
                            if audio.get("transcript"):
                                transcript_parts.append(audio["transcript"])
                        for key in ("audio", "audio_data", "data"):
                            value = source.get(key)
                            if isinstance(value, str) and _looks_base64(value):
                                chunks.append(value)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MusicGenError(f"OpenRouter HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise MusicGenError(f"OpenRouter request failed: {exc}") from exc

    audio_bytes = None
    if chunks:
        audio_bytes = base64.b64decode("".join(chunks))

    meta: dict[str, Any] = {
        "events_count": len(events),
        "transcript": "".join(transcript_parts),
        "text": "".join(text_parts),
        "final_event": final_json,
        "stream_audio_chunks": len(chunks),
    }
    return audio_bytes, meta


def _looks_base64(value: str) -> bool:
    return len(value) > 80 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value) is not None


def _download(url: str, out: Path, timeout: int) -> None:
    _require_https(url)
    req = urllib.request.Request(url, headers=_headers(auth=_is_api_url(url), accept="*/*"), method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MusicGenError(f"Download HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise MusicGenError(f"Download failed: {exc}") from exc


def _extract_audio_from_json(data: Any) -> tuple[bytes | None, str | None]:
    if isinstance(data, dict):
        for key in ("url", "audio_url", "download_url"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return None, value
        for key in ("audio", "audio_data", "data", "content"):
            value = data.get(key)
            if isinstance(value, str) and _looks_base64(value):
                return base64.b64decode(value), None
            if isinstance(value, dict):
                found, url = _extract_audio_from_json(value)
                if found or url:
                    return found, url
        for value in data.values():
            found, url = _extract_audio_from_json(value)
            if found or url:
                return found, url
    elif isinstance(data, list):
        for item in data:
            found, url = _extract_audio_from_json(item)
            if found or url:
                return found, url
    return None, None


def _audio_format_from_out(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in {"mp3", "wav", "flac", "opus", "aac"}:
        return ext
    return "mp3"


def _with_duration(prompt: str, duration: int) -> str:
    if re.search(r"\btarget duration\b|\bduration\b", prompt, re.I):
        return prompt
    return f"{prompt.strip()}\n\nTarget duration: {duration} seconds."


def _music_models(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    rows = data.get("data", data if isinstance(data, list) else [])
    result = []
    for model in rows:
        if not isinstance(model, dict):
            continue
        blob = json.dumps(model, ensure_ascii=False).lower()
        mid = str(model.get("id", "")).lower()
        if any(term in blob or term in mid for term in ("lyria", "music", "audio", "sound")):
            result.append(model)
    return result


def check_key(_: argparse.Namespace) -> int:
    key, source = _key_with_source()
    print(f"OPENROUTER_API_KEY found in {source}; length={len(key)}; value=hidden")
    return 0


def list_models(args: argparse.Namespace) -> int:
    data = _request_json("GET", f"{BASE_URL}/models", auth=False, timeout=args.timeout)
    rows = _music_models(data)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No obvious audio/music models found in /models response.")
        return 1
    for model in rows:
        mid = model.get("id", "")
        name = model.get("name", "")
        context = model.get("context_length", "")
        pricing = model.get("pricing") or {}
        print(f"{mid}\t{name}\tcontext:{context}\tprompt:{pricing.get('prompt', '-')}\tcompletion:{pricing.get('completion', '-')}")
    available = {m.get("id") for m in rows}
    print(f"default_available={DEFAULT_MODEL in available}")
    print(f"fallback_available={FALLBACK_MODEL in available}")
    return 0


def generate(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    audio_format = _audio_format_from_out(out)
    prompt = _with_duration(args.prompt, args.duration)
    payload: dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["text", "audio"],
        "audio": {"format": audio_format},
        "stream": True,
    }
    if args.extra_json:
        try:
            extra = json.loads(args.extra_json)
        except json.JSONDecodeError as exc:
            raise MusicGenError(f"--extra-json is not valid JSON: {exc}") from exc
        if not isinstance(extra, dict):
            raise MusicGenError("--extra-json must decode to an object")
        payload.update(extra)

    audio_bytes, stream_meta = _stream_chat_completion(payload, args.timeout)
    shape = "stream_audio_chunks"
    if audio_bytes:
        out.write_bytes(audio_bytes)
    else:
        found, url = _extract_audio_from_json(stream_meta)
        if found:
            out.write_bytes(found)
            shape = "base64_json"
        elif url:
            _download(url, out, args.timeout)
            shape = "url_json"
        else:
            if args.meta_out:
                _write_meta(Path(args.meta_out), {"request": _redact_payload(payload), "response": stream_meta, "unsupported_response_shape": True})
            raise MusicGenError("Unsupported OpenRouter audio response shape. Metadata was written if --meta-out was provided.")

    print(f"saved={out.resolve()}")
    if args.meta_out:
        _write_meta(
            Path(args.meta_out),
            {
                "request": _redact_payload(payload),
                "response": stream_meta,
                "model": args.model,
                "prompt": prompt,
                "duration": args.duration,
                "output": str(out.resolve()),
                "response_shape": shape,
                "generated_at_unix": int(time.time()),
            },
        )
        print(f"metadata={Path(args.meta_out).resolve()}")
    return 0


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _write_meta(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_vet_eye(args: argparse.Namespace) -> int:
    args.prompt = VET_EYE_PROMPT.replace("115 seconds", f"{args.duration} seconds")
    args.model = args.model or DEFAULT_MODEL
    return generate(args)


def probe(args: argparse.Namespace) -> int:
    if not shutil.which("ffprobe"):
        raise MusicGenError("ffprobe not found on PATH.")
    path = Path(args.input)
    if not path.exists():
        raise MusicGenError(f"Input not found: {path}")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=codec_name,codec_type,channels,sample_rate",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise MusicGenError(completed.stderr.strip() or "ffprobe failed")
    print(completed.stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Generate music beds through OpenRouter audio/music models.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ck = sub.add_parser("check-key", help="Check OPENROUTER_API_KEY without printing it.")
    ck.set_defaults(func=check_key)

    lm = sub.add_parser("list-models", help="List likely audio/music models from OpenRouter /models.")
    lm.add_argument("--json", action="store_true")
    lm.add_argument("--timeout", type=int, default=60)
    lm.set_defaults(func=list_models)

    gen = sub.add_parser("generate", help="Generate a music bed.")
    gen.add_argument("--model", default=DEFAULT_MODEL)
    gen.add_argument("--prompt", required=True)
    gen.add_argument("--duration", type=int, default=15)
    gen.add_argument("--out", required=True)
    gen.add_argument("--meta-out")
    gen.add_argument("--extra-json", help="JSON object merged into the OpenRouter request.")
    gen.add_argument("--timeout", type=int, default=1800)
    gen.set_defaults(func=generate)

    vet = sub.add_parser("generate-vet-eye", help="Generate the default Vet-Eye Smart Imaging music bed.")
    vet.add_argument("--model", default=DEFAULT_MODEL)
    vet.add_argument("--duration", type=int, default=115)
    vet.add_argument("--out", default="output/audio/vet-eye-music-bed.mp3")
    vet.add_argument("--meta-out", default="output/audio/vet-eye-music-bed.meta.json")
    vet.add_argument("--extra-json", help="JSON object merged into the OpenRouter request.")
    vet.add_argument("--timeout", type=int, default=1800)
    vet.set_defaults(func=generate_vet_eye)

    pr = sub.add_parser("probe", help="Probe an audio file with ffprobe.")
    pr.add_argument("--input", required=True)
    pr.set_defaults(func=probe)
    return ap


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except MusicGenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
