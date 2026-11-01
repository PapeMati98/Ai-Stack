# ElevenLabs API Notes

Checked 2026-07-01 against official ElevenLabs documentation.

## Authentication

Official docs say every API request should include the API key in the `xi-api-key` HTTP header. The API key is a secret and must not be exposed in client-side code or chat.

Source: `https://elevenlabs.io/docs/api-reference/authentication`

## Core Endpoints Used

- Models: `GET https://api.elevenlabs.io/v1/models`
  - Used by `list-models`.
  - Relevant fields include `model_id`, `name`, `can_do_text_to_speech`, `can_use_style`, `can_use_speaker_boost`, and max request lengths.
  - Source: `https://elevenlabs.io/docs/api-reference/models/list`

- Voices: `GET https://api.elevenlabs.io/v2/voices`
  - Used by `list-voices`.
  - Returns voice objects with `voice_id`, `name`, `category`, labels, description, and preview metadata.
  - Source: `https://elevenlabs.io/docs/api-reference/voices/search`

- Text to speech: `POST https://api.elevenlabs.io/v1/text-to-speech/:voice_id`
  - Used by `tts`.
  - Request includes required `text` and optional `model_id`, `language_code`, `voice_settings`, `seed`, `previous_text`, and `next_text`.
  - Source: `https://elevenlabs.io/docs/api-reference/text-to-speech/convert`

- User subscription: `GET https://api.elevenlabs.io/v1/user/subscription`
  - Used by `check-key`.
  - Source: `https://elevenlabs.io/docs/api-reference/user/subscription/get`

## Output Formats

ElevenLabs `output_format` is passed as a query parameter, for example `mp3_44100_128`. Some higher-bitrate MP3/WAV/PCM formats may require a paid tier. Default to `mp3_44100_128` unless the task needs a specific delivery format.

## Key Storage Policy

The skill supports three lookup paths:
1. Current process env var `ELEVENLABS_API_KEY`.
2. Windows User/Machine persistent env var `ELEVENLABS_API_KEY`.
3. DPAPI-encrypted local file written by `scripts/elevenlabs_key.ps1`.

Never store the key in project files or dotenv files unless the user explicitly chooses that for a private local project.
