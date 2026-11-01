#!/usr/bin/env python3
"""ElevenLabs voice generation helper for the voice-gen skill.

Uses the REST API with only Python standard library dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
SCRIPT_DIR = Path(__file__).resolve().parent
KEY_SCRIPT = SCRIPT_DIR / "elevenlabs_key.ps1"


class VoiceGenError(RuntimeError):
    pass


def registry_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg  # type: ignore
    except ImportError:
        return None

    locations = [
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                if value:
                    return str(value)
        except OSError:
            continue
    return None


def dpapi_key() -> str | None:
    if os.name != "nt" or not KEY_SCRIPT.exists():
        return None
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(KEY_SCRIPT),
        "get",
        "-ForSubprocess",
    ]
    try:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    key = completed.stdout.strip()
    return key or None


def load_api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY") or registry_env("ELEVENLABS_API_KEY") or dpapi_key()
    if not key:
        raise VoiceGenError(
            "Missing ELEVENLABS_API_KEY. Run: powershell -NoProfile -ExecutionPolicy Bypass "
            '-File "$env:USERPROFILE\\.claude\\skills\\voice-gen\\scripts\\elevenlabs_key.ps1" set'
        )
    return key


def api_request(
    method: str,
    path: str,
    api_key: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    binary: bool = False,
    timeout: float = 120,
) -> tuple[bytes, dict[str, str]]:
    url = f"{API_BASE}{path}"
    if query:
        clean_query = {k: v for k, v in query.items() if v is not None}
        if clean_query:
            url += "?" + urlencode(clean_query)

    data = None
    headers = {
        "xi-api-key": api_key,
        "User-Agent": "Codex voice-gen skill",
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = response.read()
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            if binary:
                return payload, response_headers
            return payload, response_headers
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise VoiceGenError(f"ElevenLabs HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise VoiceGenError(f"ElevenLabs request failed: {exc}") from exc


def as_json(payload: bytes) -> Any:
    return json.loads(payload.decode("utf-8"))


def slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return value[:80] or "voice"


def extension_for_format(output_format: str) -> str:
    codec = output_format.split("_", 1)[0].lower()
    if codec in {"mp3", "wav", "pcm", "ulaw"}:
        return codec
    return "audio"


def read_text_arg(args: argparse.Namespace) -> str:
    if args.text and args.text_file:
        raise VoiceGenError("Use either --text or --text-file, not both.")
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if args.text:
        return args.text
    raise VoiceGenError("Provide --text or --text-file.")


def command_check_key(_: argparse.Namespace) -> int:
    key = load_api_key()
    payload, _ = api_request("GET", "/v1/user/subscription", key)
    data = as_json(payload)
    tier = data.get("tier", "<unknown>")
    used = data.get("character_count")
    limit = data.get("character_limit")
    reset = data.get("next_character_count_reset_unix")
    print("ElevenLabs key OK")
    print(f"tier: {tier}")
    if used is not None and limit is not None:
        print(f"characters: {used}/{limit}")
    if reset:
        try:
            print("reset_unix:", int(reset))
        except (TypeError, ValueError):
            print("reset:", reset)
    return 0


def command_list_models(args: argparse.Namespace) -> int:
    key = load_api_key()
    payload, _ = api_request("GET", "/v1/models", key)
    models = as_json(payload)
    if args.json:
        print(json.dumps(models, ensure_ascii=False, indent=2))
        return 0
    for model in models:
        if args.tts_only and not model.get("can_do_text_to_speech"):
            continue
        flags = []
        for field, label in [
            ("can_do_text_to_speech", "tts"),
            ("can_use_style", "style"),
            ("can_use_speaker_boost", "boost"),
            ("requires_alpha_access", "alpha"),
        ]:
            if model.get(field):
                flags.append(label)
        max_len = model.get("maximum_text_length_per_request") or model.get("max_characters_request_subscribed_user")
        suffix = f" max={max_len}" if max_len else ""
        print(f"{model.get('model_id')} | {model.get('name')} | {','.join(flags) or '-'}{suffix}")
    return 0


def command_list_voices(args: argparse.Namespace) -> int:
    key = load_api_key()
    query = {"page_size": args.limit}
    payload, _ = api_request("GET", "/v2/voices", key, query=query)
    data = as_json(payload)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    voices = data.get("voices", data if isinstance(data, list) else [])
    if args.search:
        needle = args.search.lower()
        voices = [
            voice
            for voice in voices
            if needle in str(voice.get("name", "")).lower()
            or needle in json.dumps(voice.get("labels", {}), ensure_ascii=False).lower()
            or needle in str(voice.get("description", "")).lower()
        ]
    for voice in voices[: args.limit]:
        labels = voice.get("labels") or {}
        label_text = ", ".join(f"{k}={v}" for k, v in labels.items()) if isinstance(labels, dict) else ""
        print(f"{voice.get('voice_id')} | {voice.get('name')} | {voice.get('category', '-')}")
        if label_text:
            print(f"  labels: {label_text}")
        if voice.get("description"):
            print(f"  {voice.get('description')}")
    return 0


def voice_settings_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    settings: dict[str, Any] = {}
    for attr in ["stability", "similarity_boost", "style"]:
        value = getattr(args, attr)
        if value is not None:
            settings[attr] = value
    if args.use_speaker_boost is not None:
        settings["use_speaker_boost"] = args.use_speaker_boost
    return settings or None


def command_tts(args: argparse.Namespace) -> int:
    key = load_api_key()
    text = read_text_arg(args).strip()
    if not text:
        raise VoiceGenError("Text is empty.")

    output_format = args.output_format
    body: dict[str, Any] = {
        "text": text,
        "model_id": args.model,
    }
    if args.language_code:
        body["language_code"] = args.language_code
    if args.seed is not None:
        body["seed"] = args.seed
    if args.previous_text:
        body["previous_text"] = args.previous_text
    if args.next_text:
        body["next_text"] = args.next_text
    settings = voice_settings_from_args(args)
    if settings:
        body["voice_settings"] = settings

    out = Path(args.out) if args.out else Path("output") / "voice" / f"{slug(args.voice_id)}-{int(time.time())}.{extension_for_format(output_format)}"
    out.parent.mkdir(parents=True, exist_ok=True)

    payload, headers = api_request(
        "POST",
        f"/v1/text-to-speech/{args.voice_id}",
        key,
        query={"output_format": output_format},
        body=body,
        binary=True,
        timeout=args.timeout,
    )
    out.write_bytes(payload)
    print(f"wrote {out.resolve()}")
    request_id = headers.get("request-id") or headers.get("x-request-id")
    if request_id:
        print(f"request_id: {request_id}")
    print(f"voice_id: {args.voice_id}")
    print(f"model: {args.model}")
    print(f"output_format: {output_format}")
    print(f"characters: {len(text)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ElevenLabs voice generation helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-key", help="validate key and show subscription usage").set_defaults(func=command_check_key)

    p_models = sub.add_parser("list-models", help="list available ElevenLabs models")
    p_models.add_argument("--json", action="store_true")
    p_models.add_argument("--tts-only", action="store_true", default=True)
    p_models.set_defaults(func=command_list_models)

    p_voices = sub.add_parser("list-voices", help="list available ElevenLabs voices")
    p_voices.add_argument("--limit", type=int, default=20)
    p_voices.add_argument("--search")
    p_voices.add_argument("--json", action="store_true")
    p_voices.set_defaults(func=command_list_voices)

    p_tts = sub.add_parser("tts", help="generate speech from text")
    p_tts.add_argument("--voice-id", required=True)
    p_tts.add_argument("--text")
    p_tts.add_argument("--text-file")
    p_tts.add_argument("--out")
    p_tts.add_argument("--model", default=DEFAULT_MODEL)
    p_tts.add_argument("--output-format", default=DEFAULT_OUTPUT_FORMAT)
    p_tts.add_argument("--language-code")
    p_tts.add_argument("--stability", type=float)
    p_tts.add_argument("--similarity-boost", type=float)
    p_tts.add_argument("--style", type=float)
    boost = p_tts.add_mutually_exclusive_group()
    boost.add_argument("--speaker-boost", dest="use_speaker_boost", action="store_true")
    boost.add_argument("--no-speaker-boost", dest="use_speaker_boost", action="store_false")
    p_tts.set_defaults(use_speaker_boost=None)
    p_tts.add_argument("--seed", type=int)
    p_tts.add_argument("--previous-text")
    p_tts.add_argument("--next-text")
    p_tts.add_argument("--timeout", type=float, default=120)
    p_tts.set_defaults(func=command_tts)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except VoiceGenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
