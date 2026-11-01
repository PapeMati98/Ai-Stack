#!/usr/bin/env python3
"""Hedra avatar generation helper for the avatar-gen skill.

Uses only the Python standard library. Never logs API keys or request headers.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


API_BASE = os.environ.get("HEDRA_API_BASE", "https://api.hedra.com/web-app/public").rstrip("/")
DEFAULT_MODEL_ID = "26f0fc66-152b-40ab-abed-76c43df99bc8"
DEFAULT_PROMPT = (
    "A calm professional talking avatar speaking directly to the camera. "
    "Natural lip-sync, stable head position, subtle facial expression, no text, no logos."
)
DOG_TOR_PROMPT = (
    "DOG TOR is a friendly, professional dog doctor avatar wearing glasses, a white medical coat "
    "and a stethoscope. He speaks directly to the camera as a calm product-demo presenter. "
    "Natural lip-sync, stable head position, subtle facial expression, no exaggerated gestures, "
    "no cartoon deformation, no text, no logos, no extra characters. Preserve the dog identity, "
    "glasses, coat and professional medical-tech tone."
)
SCRIPT_DIR = Path(__file__).resolve().parent
KEY_SCRIPT = SCRIPT_DIR / "hedra_key.ps1"


class AvatarGenError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def registry_env(name: str, scope: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg  # type: ignore
    except ImportError:
        return None

    if scope == "User":
        root, path = winreg.HKEY_CURRENT_USER, "Environment"
    elif scope == "Machine":
        root, path = winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    else:
        raise ValueError(f"Unsupported registry scope: {scope}")

    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip() or None
    except OSError:
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


def get_hedra_api_key() -> str:
    key = (
        os.environ.get("HEDRA_API_KEY")
        or registry_env("HEDRA_API_KEY", "User")
        or registry_env("HEDRA_API_KEY", "Machine")
        or dpapi_key()
    )
    if not key:
        raise AvatarGenError(
            'HEDRA_API_KEY is not configured. Run: powershell -NoProfile -ExecutionPolicy Bypass '
            '-File "$env:USERPROFILE\\.claude\\skills\\avatar-gen\\scripts\\hedra_key.ps1" set'
        )
    return key


def endpoint(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(API_BASE + "/", path.lstrip("/"))


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if "api_key" in lowered or "x-api-key" in lowered or lowered in {"authorization", "headers"}:
                clean[key] = "<redacted>"
            elif "url" in lowered:
                clean[key] = "<redacted-url>"
            else:
                clean[key] = redact_json(item)
        return clean
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


def write_json(path: str | Path | None, data: Any) -> None:
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(redact_json(data), ensure_ascii=False, indent=2), encoding="utf-8")


def http_json(method: str, path: str, api_key: str, body: dict[str, Any] | None = None, timeout: float = 120) -> Any:
    data = None
    headers = {
        "X-API-Key": api_key,
        "User-Agent": "Codex avatar-gen skill",
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(endpoint(path), data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AvatarGenError(f"Hedra HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise AvatarGenError(f"Hedra request failed: {exc}") from exc
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def http_multipart_upload(path: str, api_key: str, file_path: Path, field_name: str = "file", timeout: float = 300) -> Any:
    if not file_path.exists():
        raise AvatarGenError(f"File not found: {file_path}")

    boundary = "----codex-avatar-gen-" + uuid.uuid4().hex
    mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    file_bytes = file_path.read_bytes()
    chunks = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8"),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    data = b"".join(chunks)
    headers = {
        "X-API-Key": api_key,
        "User-Agent": "Codex avatar-gen skill",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    req = Request(endpoint(path), data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AvatarGenError(f"Hedra upload HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise AvatarGenError(f"Hedra upload failed: {exc}") from exc
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def extract_id(payload: Any, *names: str) -> str:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for name in names:
            candidates.append(payload.get(name))
        data = payload.get("data")
        if isinstance(data, dict):
            for name in names:
                candidates.append(data.get(name))
        asset = payload.get("asset")
        if isinstance(asset, dict):
            for name in names:
                candidates.append(asset.get(name))
    for candidate in candidates:
        if candidate:
            return str(candidate)
    raise AvatarGenError(f"Could not find id field {names} in response: {json.dumps(redact_json(payload), ensure_ascii=False)[:1000]}")


def extract_status(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("status", "state"):
            if payload.get(key):
                return str(payload[key]).lower()
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("status", "state"):
                if data.get(key):
                    return str(data[key]).lower()
    return "unknown"


def extract_download_url(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("download_url", "downloadUrl", "url", "video_url", "videoUrl"):
            if payload.get(key):
                return str(payload[key])
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("download_url", "downloadUrl", "url", "video_url", "videoUrl"):
                if data.get(key):
                    return str(data[key])
    return None


def download_url(url: str, out: Path, api_key: str | None = None, timeout: float = 300) -> None:
    headers = {"User-Agent": "Codex avatar-gen skill"}
    if api_key and url.startswith(API_BASE):
        headers["X-API-Key"] = api_key
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AvatarGenError(f"Download HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise AvatarGenError(f"Download failed: {exc}") from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)


def upload_asset(api_key: str, asset_type: str, file_path: Path, name: str | None, timeout: float = 300) -> tuple[str, dict[str, Any]]:
    create_body = {"name": name or file_path.name, "type": asset_type}
    create_response = http_json("POST", "/assets", api_key, create_body, timeout=timeout)
    asset_id = extract_id(create_response, "id", "asset_id", "assetId")
    upload_response = http_multipart_upload(f"/assets/{asset_id}/upload", api_key, file_path, timeout=timeout)
    return asset_id, {"create": create_response, "upload": upload_response}


def poll_generation(api_key: str, generation_id: str, interval: float, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    last_payload: dict[str, Any] = {}
    while True:
        payload = http_json("GET", f"/generations/{generation_id}/status", api_key, timeout=120)
        if not isinstance(payload, dict):
            raise AvatarGenError("Unexpected Hedra status response.")
        last_payload = payload
        status = extract_status(payload)
        print(f"generation_id={generation_id} status={status}")
        if status in {"complete", "completed", "succeeded", "success", "done"}:
            return payload
        if status in {"failed", "failure", "error", "cancelled", "canceled"}:
            raise AvatarGenError(f"Hedra generation failed: {json.dumps(redact_json(payload), ensure_ascii=False)[:2000]}")
        if time.monotonic() - started > timeout:
            raise AvatarGenError(f"Timed out waiting for generation {generation_id}. Last status: {status}")
        time.sleep(interval)


def build_generation_body(args: argparse.Namespace, image_asset_id: str, audio_asset_id: str) -> dict[str, Any]:
    return {
        "type": "video",
        "ai_model_id": args.model_id,
        "start_keyframe_id": image_asset_id,
        "audio_id": audio_asset_id,
        "generated_video_inputs": {
            "text_prompt": args.prompt,
            "aspect_ratio": args.aspect_ratio,
            "resolution": args.resolution,
            "duration_ms": args.duration_ms,
        },
    }


def run_generate(args: argparse.Namespace) -> dict[str, Any]:
    api_key = get_hedra_api_key()
    image_path = Path(args.image)
    audio_path = Path(args.audio)
    if not image_path.exists():
        raise AvatarGenError(f"Image not found: {image_path}")
    if not audio_path.exists():
        raise AvatarGenError(f"Audio not found: {audio_path}")

    print("uploading image asset")
    image_asset_id, image_meta = upload_asset(api_key, "image", image_path, image_path.name, timeout=args.timeout)
    print(f"image_asset_id={image_asset_id}")
    print("uploading audio asset")
    audio_asset_id, audio_meta = upload_asset(api_key, "audio", audio_path, audio_path.name, timeout=args.timeout)
    print(f"audio_asset_id={audio_asset_id}")

    body = build_generation_body(args, image_asset_id, audio_asset_id)
    generation_response = http_json("POST", "/generations", api_key, body, timeout=args.timeout)
    generation_id = extract_id(generation_response, "id", "generation_id", "generationId")
    print(f"generation_id={generation_id}")

    final_status = poll_generation(api_key, generation_id, args.poll_interval, args.timeout)
    video_url = extract_download_url(final_status) or extract_download_url(generation_response)
    if not video_url:
        raise AvatarGenError("Generation completed but no download_url/url was returned.")
    out = Path(args.out)
    download_url(video_url, out, api_key=api_key, timeout=args.timeout)
    print(f"wrote {out.resolve()}")

    meta = {
        "provider": "hedra",
        "base_url": API_BASE,
        "created_at": now_iso(),
        "model_id": args.model_id,
        "generation_id": generation_id,
        "image_asset_id": image_asset_id,
        "audio_asset_id": audio_asset_id,
        "prompt": args.prompt,
        "duration_ms": args.duration_ms,
        "aspect_ratio": args.aspect_ratio,
        "resolution": args.resolution,
        "output": str(out.resolve()),
        "status": final_status,
        "responses": {
            "image_asset": image_meta,
            "audio_asset": audio_meta,
            "generation": generation_response,
        },
    }
    write_json(args.meta_out, meta)
    return meta


def command_check_key(_: argparse.Namespace) -> int:
    api_key = get_hedra_api_key()
    models = http_json("GET", "/models", api_key, timeout=60)
    count = len(models) if isinstance(models, list) else len(models.get("data", [])) if isinstance(models, dict) else 0
    print("Hedra key OK")
    print(f"models_visible: {count}")
    return 0


def command_list_models(args: argparse.Namespace) -> int:
    api_key = get_hedra_api_key()
    models = http_json("GET", "/models", api_key, timeout=60)
    write_json(args.meta_out, {"provider": "hedra", "base_url": API_BASE, "created_at": now_iso(), "models": models})
    if args.json:
        print(json.dumps(redact_json(models), ensure_ascii=False, indent=2))
        return 0
    rows = models.get("data", models) if isinstance(models, dict) else models
    if isinstance(rows, list):
        for model in rows:
            if isinstance(model, dict):
                print(f"{model.get('id') or model.get('model_id') or '<no-id>'} | {model.get('name') or model.get('title') or '<no-name>'}")
            else:
                print(model)
    else:
        print(json.dumps(redact_json(models), ensure_ascii=False, indent=2))
    return 0


def command_upload_asset(args: argparse.Namespace) -> int:
    api_key = get_hedra_api_key()
    asset_id, meta = upload_asset(api_key, args.type, Path(args.file), args.name, timeout=args.timeout)
    output = {
        "provider": "hedra",
        "base_url": API_BASE,
        "created_at": now_iso(),
        "asset_id": asset_id,
        "type": args.type,
        "file": str(Path(args.file).resolve()),
        "name": args.name,
        "response": meta,
    }
    write_json(args.meta_out, output)
    print(f"asset_id={asset_id}")
    return 0


def command_generate(args: argparse.Namespace) -> int:
    run_generate(args)
    return 0


def command_status(args: argparse.Namespace) -> int:
    api_key = get_hedra_api_key()
    payload = http_json("GET", f"/generations/{args.generation_id}/status", api_key, timeout=60)
    write_json(args.meta_out, {"provider": "hedra", "base_url": API_BASE, "created_at": now_iso(), "status": payload})
    print(json.dumps(redact_json(payload), ensure_ascii=False, indent=2))
    return 0


def command_download(args: argparse.Namespace) -> int:
    api_key = get_hedra_api_key()
    payload = http_json("GET", f"/generations/{args.generation_id}/status", api_key, timeout=60)
    video_url = extract_download_url(payload)
    if not video_url:
        raise AvatarGenError("No download_url/url found in generation status.")
    out = Path(args.out)
    download_url(video_url, out, api_key=api_key, timeout=args.timeout)
    print(f"wrote {out.resolve()}")
    return 0


def command_proof_dog_tor(args: argparse.Namespace) -> int:
    if not args.prompt:
        args.prompt = DOG_TOR_PROMPT
    run_generate(args)
    return 0


def add_generation_args(parser: argparse.ArgumentParser, *, dogtor_defaults: bool = False) -> None:
    parser.add_argument("--image", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--prompt", default=DOG_TOR_PROMPT if dogtor_defaults else DEFAULT_PROMPT)
    parser.add_argument("--duration-ms", type=int, required=not dogtor_defaults, default=20000 if dogtor_defaults else None)
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--resolution", default="720p")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--out", required=True)
    parser.add_argument("--meta-out")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=900.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hedra audio-driven avatar generation helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-key", help="validate Hedra key without printing it").set_defaults(func=command_check_key)

    p_models = sub.add_parser("list-models", help="list Hedra models")
    p_models.add_argument("--json", action="store_true")
    p_models.add_argument("--meta-out")
    p_models.set_defaults(func=command_list_models)

    p_upload = sub.add_parser("upload-asset", help="upload image or audio asset")
    p_upload.add_argument("--type", choices=["audio", "image"], required=True)
    p_upload.add_argument("--file", required=True)
    p_upload.add_argument("--name", required=True)
    p_upload.add_argument("--meta-out")
    p_upload.add_argument("--timeout", type=float, default=300.0)
    p_upload.set_defaults(func=command_upload_asset)

    p_generate = sub.add_parser("generate", help="generate talking avatar")
    add_generation_args(p_generate)
    p_generate.set_defaults(func=command_generate)

    p_status = sub.add_parser("status", help="show generation status")
    p_status.add_argument("--generation-id", required=True)
    p_status.add_argument("--meta-out")
    p_status.set_defaults(func=command_status)

    p_download = sub.add_parser("download", help="download a completed generation")
    p_download.add_argument("--generation-id", required=True)
    p_download.add_argument("--out", required=True)
    p_download.add_argument("--timeout", type=float, default=300.0)
    p_download.set_defaults(func=command_download)

    p_dogtor = sub.add_parser("proof-dog-tor", help="generate a short DOG TOR lip-sync proof")
    add_generation_args(p_dogtor, dogtor_defaults=True)
    p_dogtor.set_defaults(func=command_proof_dog_tor)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except AvatarGenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
