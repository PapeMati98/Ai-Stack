"""Explicit creator: run the real local synthetic CLI, never a model or shell."""
import argparse
from pathlib import Path
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Explicit synthetic DOTS creator.")
    parser.add_argument("--runtime", help="Absolute agent-stack/shared/dots-farm/dots.py; required outside origin.")
    parser.add_argument("--state", required=True)
    parser.add_argument("dot_id")
    parser.add_argument("--kind", required=True, choices=("observer", "collector"))
    parser.add_argument("--org", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--budget", type=int, default=2000000)
    parser.add_argument("--interval", type=float, default=900)
    parser.add_argument("--source")
    args = parser.parse_args()
    runtime = Path(args.runtime) if args.runtime else Path(__file__).resolve().parents[5] / "shared/dots-farm/dots.py"
    if (not runtime.is_absolute() or runtime.name != "dots.py" or not runtime.is_file()
            or not (runtime.parent / "dots_farm/engine.py").is_file() or not Path(args.state).is_absolute()):
        print('{"error":"CREATOR_RUNTIME_OR_STATE_REQUIRED"}', file=sys.stderr)
        return 2
    argv = [sys.executable, "-B", str(runtime), "--state", args.state]
    if args.source:
        argv += ["--source", args.source]
    argv += ["create", args.dot_id, "--kind", args.kind, "--org", args.org, "--project", args.project,
             "--budget", str(args.budget), "--interval", str(args.interval)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
