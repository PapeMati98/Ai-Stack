"""Maintainer CLI. No publication, deployment or host-state discovery command."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .configuration import validate_config
from .exporter import export_candidate, verify_export
from .integration import _safe_path, bundle_check, load_json, native_uat, new_output, offline_demo, write_json
from .privacy import scan_tree
from .provenance import audit_provenance


def parser():
    root = argparse.ArgumentParser(description="Private release workbench; local checks never authorize publication.")
    commands = root.add_subparsers(dest="command", required=True)
    options = {
        "export": ("manifest", "destination", "output-parent", "receipt"),
        "verify": ("candidate", "receipt"),
        "scan": ("candidate", "report"),
        "config-check": ("config", "owned-root"),
        "rights-check": ("manifest", "components", "report"),
        "bundle-check": ("candidate", "receipt", "report"),
        "demo": ("workspace", "source", "report"),
        "native-uat": ("candidate", "receipt", "config", "workspace", "plan", "report"),
    }
    for command, flags in options.items():
        sub = commands.add_parser(command)
        for flag in flags:
            sub.add_argument("--" + flag, required=True, type=Path)
        if command == "native-uat":
            sub.add_argument("--execute", action="store_true",
                             help="Parent only after independent review: invoke the exported OS isolation runner.")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    output = None
    try:
        if args.command == "export":
            output = new_output(args.receipt, outside=args.destination)
        elif hasattr(args, "report"):
            output = new_output(args.report, outside=getattr(args, "candidate", None))
            if args.command in {"demo", "native-uat"} and output.is_relative_to(_safe_path(args.workspace)):
                raise ValueError("CLI report must be outside the task workspace")
        if args.command == "export":
            manifest = load_json(args.manifest)
            for source in manifest["sources"]:
                _safe_path(source["repo"])
            result = export_candidate(manifest, _safe_path(args.destination),
                                      allowed_output_parent=_safe_path(args.output_parent))
        elif args.command == "verify":
            result = verify_export(_safe_path(args.candidate), load_json(args.receipt))
        elif args.command == "scan":
            result = scan_tree(_safe_path(args.candidate))
        elif args.command == "config-check":
            result = validate_config(load_json(args.config), owned_root=_safe_path(args.owned_root))
        elif args.command == "rights-check":
            result = audit_provenance(load_json(args.manifest), load_json(args.components))
        elif args.command == "bundle-check":
            result = bundle_check(args.candidate, load_json(args.receipt))
        elif args.command == "demo":
            result = offline_demo(args.workspace, args.source)
        else:
            result = native_uat(args.candidate, load_json(args.receipt), load_json(args.config),
                                args.workspace, load_json(args.plan), execute=args.execute)
        if output is not None:
            write_json(output, result)
    except (ValueError, OSError, TypeError, KeyError):
        # Library errors can contain caller-supplied component labels or paths.
        # Never echo those, native logs, or matched secret values.
        result = {"version": 1, "status": "FAIL", "operation": args.command,
                  "error": "invalid_or_unavailable_input_or_output", "public_ready": False}
        # Preserve useful machine-readable failure evidence, without touching an
        # existing report or retrying a failed report write.
        if output is not None and not output.exists():
            try:
                write_json(output, result)
            except (ValueError, OSError):
                pass
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    if result["status"] in {"PASS", "exported", "verified", "PREPARED", "EXECUTED"}:
        return 0
    return 2 if result["status"] in {"OPEN", "PENDING"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
