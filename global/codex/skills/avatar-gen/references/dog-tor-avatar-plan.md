# DOG TOR Avatar Plan

Use this reference when upgrading the Vet-Eye Smart Imaging film with a speaking DOG TOR avatar.

## Decision

DOG TOR must be generated with a dedicated audio-driven lip-sync/avatar tool. Do not keep patching talking mouth animation with generic `video-gen` prompts. `video-gen` can still be used for intro, outro, neutral transitions, and b-roll.

## Working Directory

Use:

```text
outputs/dog_tor_avatar_v4/
outputs/dog_tor_avatar_v4/assets/
outputs/dog_tor_avatar_v4/audio/
outputs/dog_tor_avatar_v4/avatar/
outputs/dog_tor_avatar_v4/frames/
outputs/dog_tor_avatar_v4/subtitles/
outputs/dog_tor_avatar_v4/final/
outputs/dog_tor_avatar_v4/meta/
outputs/dog_tor_avatar_v4/proof/
```

Copy source files:

```text
assets/source_demo.mp4
assets/dog_tor_reference.png
```

Use the clean source demo as the montage base. Do not use v1/v2/v3 renders as source truth.

## Naming

The character name is `DOG TOR`.

Accepted Polish forms:

- DOG TOR
- DOG TORA
- DOG TOROWI
- z DOG TOREM
- o DOG TORZE

Never show `DOG tora` as a proper name.

## Synchronization Rule

Fix narration timing before generating the avatar.

1. Identify visual beats in the clean source video.
2. Write short narration segments that match those beats.
3. Generate natural TTS segments without clipped words.
4. If narration is longer than a visual beat, hold/freeze the visual rather than cutting the voice.
5. If narration is shorter, leave a natural pause.
6. Generate DOG TOR from the final mixed narration audio, not from text.

## Required Proof

Generate a 15-25 second DOG TOR proof first. A good test range is around the real prediction section of the demo, not only the intro.

Example:

```powershell
python (Join-Path $SkillRoot 'scripts\avatar_gen.py') proof-dog-tor `
  --image "outputs\dog_tor_avatar_v4\assets\dog_tor_reference.png" `
  --audio "outputs\dog_tor_avatar_v4\audio\dog_tor_voiceover_final.wav" `
  --out "outputs\dog_tor_avatar_v4\proof\dog_tor_avatar_lipsync_20s.mp4" `
  --duration-ms 20000 `
  --meta-out "outputs\dog_tor_avatar_v4\meta\dog_tor_avatar_lipsync_20s.meta.json"
```

## Proof Acceptance Criteria

The proof passes only when:

- DOG TOR keeps the dog-doctor identity.
- Mouth movement follows the rhythm of the exact audio used in the film.
- Eyes, nose, glasses, coat, and stethoscope do not deform.
- There is no second mouth, second head, strange teeth, or human mouth pasted onto the dog.
- Motion is not random image-to-video drift.
- Any watermark is reported.
- The avatar still looks good when scaled down to a 220-280 px inset.

If the proof fails, do not render the full film. Try one fallback provider if available, then report the limitation.

## Montage Layout

For the film proof:

- Use clean source video.
- Place DOG TOR as a small inset, usually bottom-right.
- Use 220-260 px width and a stable position.
- Add a subtle teal frame only if it improves readability.
- Do not cover key UI fields or model results.
- Avoid a large side panel that makes the demo unreadable.

## Final Render Gate

Full render is allowed only after the proof passes.

Expected full output:

```text
outputs/dog_tor_avatar_v4/final/vet_eye_smart_imaging_demo_dog_tor_v4.mp4
```

Export target:

- MP4
- H.264
- AAC
- 1920x1080
- 30 fps
- yuv420p
- CRF 18-22

Do not represent Smart Imaging as diagnosis. It is a non-diagnostic operator support demonstrator.
