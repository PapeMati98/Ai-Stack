# Hedra API Reference for avatar-gen

Checked against official Hedra developer documentation on 2026-07-02.

## Purpose

Use Hedra for audio-driven avatar generation when a still avatar image and final voiceover audio must produce a lip-synced talking presenter. This is the correct path for DOG TOR style narration. Do not use general image-to-video models for mouth animation.

## Auth and Base URL

- Base URL: `https://api.hedra.com/web-app/public`
- Auth header: `X-API-Key: <api-key>`
- Local env name: `HEDRA_API_KEY`
- Optional override for testing: `HEDRA_API_BASE`

Never write the API key to prompts, manifests, CLI args, config JSON, logs, request dumps, or shell history examples.

## Default Model

- Hedra Avatar model id: `26f0fc66-152b-40ab-abed-76c43df99bc8`
- Default aspect ratio: `1:1`
- Default resolution: `720p`

## Endpoint Map

List models:

```text
GET /models
```

Create asset:

```text
POST /assets
Content-Type: application/json

{
  "name": "dog_tor_reference.png",
  "type": "image"
}
```

Upload asset file:

```text
POST /assets/{asset_id}/upload
Content-Type: multipart/form-data
field: file
```

Start generation:

```text
POST /generations
Content-Type: application/json

{
  "type": "video",
  "ai_model_id": "26f0fc66-152b-40ab-abed-76c43df99bc8",
  "start_keyframe_id": "{image_asset_id}",
  "audio_id": "{audio_asset_id}",
  "generated_video_inputs": {
    "text_prompt": "...",
    "aspect_ratio": "1:1",
    "resolution": "720p",
    "duration_ms": 20000
  }
}
```

Poll generation:

```text
GET /generations/{generation_id}/status
```

Download:

- Prefer `download_url` from status response.
- Fall back to `url` when returned.
- If neither exists, save status metadata and report the missing download URL.

## Manifest Rules

Allowed fields:

- provider: `hedra`
- base_url
- model_id
- generation_id
- image_asset_id
- audio_asset_id
- output path
- prompt
- duration_ms
- aspect_ratio
- resolution
- status
- created_at
- non-secret response metadata

Forbidden fields:

- `HEDRA_API_KEY`
- `X-API-Key`
- raw request headers
- full curl commands with headers
- plaintext secrets

## Source Links

- Hedra developer docs: `https://hedra.com/docs/`
- Generate video guide: `https://hedra.com/docs/pages/developer/guides/generate-video`
- API reference: `https://hedra.com/docs/api-reference/public`
