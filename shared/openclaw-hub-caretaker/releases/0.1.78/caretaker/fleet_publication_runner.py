"""Run canonical Fleet with a size hint for its unchanged state reader.

The reader's original validation, quota, identity and CLI stay authoritative.
Small telemetry files must not request a64MiB buffer per file. Only this
publication child uses the hint; no installed Fleet source is edited.
"""
from __future__ import annotations

import argparse
import faulthandler
import runpy
import sys
from pathlib import Path


def sized_state_reader(original):
    def read(path, *, max_bytes, encoding="utf-8"):
        try:
            size = Path(path).lstat().st_size
        except OSError:
            # Keep canonical missing/permission diagnostics unchanged.
            return original(path, max_bytes=max_bytes, encoding=encoding)
        # The canonical reader still validates regularity/reparse/size and JSON.
        # Growth between observations is refused by its existing limit guard.
        return original(path, max_bytes=min(max_bytes, size), encoding=encoding)
    return read


def _run_fleet():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fleet-script", required=True)
    args, fleet_args = parser.parse_known_args()
    fleet = Path(args.fleet_script).resolve(strict=True)
    if fleet.name != "fleet.py" or fleet.parent.name != "scripts":
        raise ValueError("invalid canonical Fleet script")
    # Parent selected an attested runtime. Execute that same canonical CLI;
    # profiles, auth, quota, model identity and all hard gates remain native.
    own_directory = Path(__file__).resolve().parent
    # Executing this file as a script puts caretaker/ first on sys.path.
    # Remove it so caretaker/secrets.py cannot shadow stdlib secrets.
    sys.path[:] = [item for item in sys.path
                   if Path(item or ".").resolve() != own_directory]
    sys.path.insert(0, str(fleet.parent))
    import adaptive
    adaptive.read_state_json = sized_state_reader(adaptive.read_state_json)
    sys.argv = [str(fleet), *fleet_args]
    runpy.run_path(str(fleet), run_name="__main__")


def main():
    # This diagnostic contains stack frames only; it never captures locals,
    # prompts, environment values, or request bodies.
    try:
        trace_file = (Path.cwd() / ".fleet_publication_runner.tracebacks.log").open(
            "ab", buffering=0,
        )
    except OSError:
        trace_file = None
    if trace_file is not None:
        try:
            faulthandler.dump_traceback_later(30, repeat=True, file=trace_file)
        except (OSError, RuntimeError, ValueError):
            trace_file.close()
            trace_file = None
    try:
        _run_fleet()
    finally:
        if trace_file is not None:
            faulthandler.cancel_dump_traceback_later()
            trace_file.close()


if __name__ == "__main__":
    main()
