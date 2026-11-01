# Music Bed Direction

Use this reference when preparing prompts and mixes for narration-safe background music.

## Prompt Principles

- Say `original instrumental` explicitly.
- Say `no vocals, no lyrics, no choir, no spoken words`.
- Describe function before genre: background bed under narration, intro stinger, loop, product demo transition, outro.
- Keep lead melody restrained for narrated content.
- Mention target duration and section structure.
- Avoid artist names, song names, labels, and copyrighted references.
- Prefer mood/instrumentation/tempo over genre imitation.

## Narration-Safe Defaults

- Tempo: 80-100 BPM.
- Stereo, clean, low distraction.
- Sparse midrange so voice-over remains intelligible.
- Avoid busy percussion, sharp transients, big risers, trailer drums, loud bass drops, and prominent vocal textures.
- Use gentle movement: soft synth pads, felt piano, pulse, light electronic texture, warm low synth, restrained percussion.

## Mix Defaults

- Voice-over target: about -16 LUFS.
- Music under narration: about -30 LUFS.
- Final mix: about -16 LUFS.
- Music fade-in: 1.5 s.
- Music fade-out: 2.5 s.
- Final standalone audio: WAV 48 kHz.
- Muxed MP4 audio: AAC 48 kHz.

Prefer sidechain ducking if ffmpeg supports the filter graph. Safe fallback:
1. normalize voice-over;
2. lower music;
3. loop/trim/fade music;
4. `amix`;
5. final `loudnorm`.

## Quality Checks

- Probe duration, codec, channels, sample rate.
- Listen for vocals/lyrics or lead melody fighting speech.
- Check that intro/outro do not mask first/last spoken words.
- For executive demos, avoid comedy, horror, childish pet-video tone, and dramatic hospital-drama music.
