#!/usr/bin/env python3
"""ffmpeg audio mixing helper for audio-bed-gen."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


class MixError(RuntimeError):
    pass


def require(tool: str) -> None:
    if not shutil.which(tool):
        raise MixError(f"{tool} not found on PATH.")


def run(cmd: list[str]) -> None:
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise MixError(completed.stderr.strip() or f"command failed: {' '.join(cmd)}")


def mix(args: argparse.Namespace) -> int:
    require("ffmpeg")
    voice = Path(args.voiceover)
    music = Path(args.music)
    out = Path(args.out)
    if not voice.exists():
        raise MixError(f"voiceover not found: {voice}")
    if not music.exists():
        raise MixError(f"music not found: {music}")
    out.parent.mkdir(parents=True, exist_ok=True)

    duration = args.duration
    fade_out_start = max(duration - 2.5, 0.0)
    music_volume = args.music_volume
    filter_complex = (
        f"[0:a]loudnorm=I={args.voice_lufs}:TP=-1.5:LRA=11,aresample=48000[voice];"
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=0:{duration},asetpts=N/SR/TB,"
        f"volume={music_volume},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_start}:d=2.5,"
        f"loudnorm=I={args.music_lufs}:TP=-2.0:LRA=18,aresample=48000[music];"
        f"[voice][music]amix=inputs=2:duration=first:dropout_transition=0,"
        f"loudnorm=I={args.final_lufs}:TP=-1.5:LRA=11[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(voice),
        "-stream_loop",
        "-1",
        "-i",
        str(music),
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-t",
        str(duration),
        "-ar",
        "48000",
        "-ac",
        "2",
        str(out),
    ]
    run(cmd)
    print(f"mixed={out.resolve()}")
    return 0


def mux_video(args: argparse.Namespace) -> int:
    require("ffmpeg")
    video = Path(args.video)
    audio = Path(args.audio)
    out = Path(args.out)
    if not video.exists():
        raise MixError(f"video not found: {video}")
    if not audio.exists():
        raise MixError(f"audio not found: {audio}")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-shortest",
        str(out),
    ]
    run(cmd)
    print(f"muxed={out.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Mix voice-over with generated music and mux audio into video.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    mx = sub.add_parser("mix", help="Mix voice-over with music bed.")
    mx.add_argument("--voiceover", required=True)
    mx.add_argument("--music", required=True)
    mx.add_argument("--out", required=True)
    mx.add_argument("--duration", type=float, required=True)
    mx.add_argument("--music-volume", type=float, default=0.18)
    mx.add_argument("--voice-lufs", type=float, default=-16)
    mx.add_argument("--music-lufs", type=float, default=-30)
    mx.add_argument("--final-lufs", type=float, default=-16)
    mx.set_defaults(func=mix)

    mv = sub.add_parser("mux-video", help="Mux mixed audio into a video file.")
    mv.add_argument("--video", required=True)
    mv.add_argument("--audio", required=True)
    mv.add_argument("--out", required=True)
    mv.set_defaults(func=mux_video)
    return ap


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except MixError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
