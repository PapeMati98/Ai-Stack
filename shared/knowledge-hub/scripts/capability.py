#!/usr/bin/env python3
"""Read-only Capability Hub verifier and TEMP-only Git materializer."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from capability_core import (
    CAPABILITY_MARKER_NAME,
    CapabilityError,
    GIT_OID,
    MARKER_NAMES,
    SECURITY_STATEMENT,
    absolute_plain_path,
    find_managed_marker,
    inspect_git_source,
    materialize_git_source,
    parse_iso_timestamp,
    read_marker,
    scan_directory,
    verify_target,
)


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    source = subparsers.add_parser("source-inspect", help="Inspect a pinned Git tree")
    source.add_argument("--repo", required=True)
    source.add_argument("--commit", required=True)
    source.add_argument("--source-path", required=True)
    source.add_argument("--policy", choices=("pilot", "existing"), default="pilot")

    materialize = subparsers.add_parser(
        "materialize", help="Materialize exact Git blobs below system TEMP"
    )
    materialize.add_argument("--repo", required=True)
    materialize.add_argument("--commit", required=True)
    materialize.add_argument("--source-path", required=True)
    materialize.add_argument("--output")

    marker = subparsers.add_parser("marker-check", help="Parse a marker without writes")
    marker.add_argument("--marker", required=True)

    scan = subparsers.add_parser("scan", help="Hash a directory without writes")
    scan.add_argument("--target", required=True)

    verify = subparsers.add_parser("verify", help="Verify one installed target without writes")
    verify.add_argument("--target", required=True)
    verify.add_argument("--tool", required=True)
    verify.add_argument("--repo")
    verify.add_argument("--commit")
    verify.add_argument("--source-path")
    verify.add_argument("--activation-boundary", default="unknown")
    verify.add_argument("--last-fetch")

    local = subparsers.add_parser(
        "verify-local", help="Verify known local adapters without modifying them"
    )
    local.add_argument("--hub-root", required=True)
    local.add_argument("--commit", default="HEAD")
    local.add_argument(
        "--tools",
        default="codex,kimi-work",
        help="Comma-separated subset of codex,claude,kimi,antigravity,grok,qwen,kimi-work",
    )

    status = subparsers.add_parser(
        "status", help="Enumerate managed local capabilities without writes"
    )
    status.add_argument("--all-managed", action="store_true")
    status.add_argument("--hub-root")
    status.add_argument("--catalog-commit")
    status.add_argument("--last-fetch")
    status.add_argument(
        "--tools",
        default="codex,claude,kimi,antigravity,grok,qwen,kimi-work",
        help="Comma-separated subset of codex,claude,kimi,antigravity,grok,qwen,kimi-work",
    )
    return parser


def local_specs(hub_root: Path) -> dict[str, dict[str, str]]:
    profile = Path.home()
    appdata = Path(os.environ.get("APPDATA", profile / "AppData/Roaming"))
    canonical = "skills/llm-wiki"
    alias = "skills/shared-agent-knowledge"
    return {
        "codex": {
            "target": str(profile / ".codex/skills/llm-wiki"),
            "source_path": canonical,
            "activation_boundary": "new Codex task/session",
        },
        "claude": {
            "target": str(profile / ".claude/skills/llm-wiki"),
            "source_path": canonical,
            "activation_boundary": "new Claude Code session",
        },
        "kimi": {
            "target": str(profile / ".agents/skills/llm-wiki"),
            "source_path": canonical,
            "activation_boundary": "new Kimi Code session",
        },
        "antigravity": {
            "target": str(profile / ".gemini/config/skills/llm-wiki"),
            "source_path": canonical,
            "activation_boundary": "new Antigravity session",
        },
        "grok": {
            "target": str(profile / ".grok/skills/llm-wiki"),
            "source_path": canonical,
            "activation_boundary": "new Grok CLI session",
        },
        "qwen": {
            "target": str(profile / ".qwen/skills/llm-wiki"),
            "source_path": canonical,
            "activation_boundary": "new Qwen Code session",
        },
        "kimi-work": {
            "target": str(
                appdata
                / "kimi-desktop/daimon-share/daimon/skills/shared-agent-knowledge"
            ),
            "source_path": alias,
            "activation_boundary": "new Kimi Desktop session; no forced restart",
        },
    }


def local_scan_roots() -> dict[str, dict[str, str]]:
    profile = Path.home()
    appdata = Path(os.environ.get("APPDATA", profile / "AppData/Roaming"))
    return {
        "codex": {
            "root": str(profile / ".codex/skills"),
            "activation_boundary": "new Codex task/session",
        },
        "claude": {
            "root": str(profile / ".claude/skills"),
            "activation_boundary": "new Claude Code session",
        },
        "kimi": {
            "root": str(profile / ".agents/skills"),
            "activation_boundary": "new Kimi Code session",
        },
        "antigravity": {
            "root": str(profile / ".gemini/config/skills"),
            "activation_boundary": "new Antigravity session",
        },
        "grok": {
            "root": str(profile / ".grok/skills"),
            "activation_boundary": "new Grok CLI session",
        },
        "qwen": {
            "root": str(profile / ".qwen/skills"),
            "activation_boundary": "new Qwen Code session",
        },
        "kimi-work": {
            "root": str(appdata / "kimi-desktop/daimon-share/daimon/skills"),
            "activation_boundary": "new Kimi Desktop session; no forced restart",
        },
    }


def _catalog_revocation(
    result: dict[str, object],
    marker_data: dict[str, object],
    *,
    hub_root: Path | None,
    catalog_commit: str | None,
    last_fetch: str | None,
) -> dict[str, object]:
    if result.get("status") not in {"CURRENT", "STALE_VIEW"}:
        return result
    if hub_root is None or catalog_commit is None or last_fetch is None:
        return {
            **result,
            "status": "STALE_VIEW",
            "reason": "revocation catalog commit and last-fetch timestamp are required",
        }
    parse_iso_timestamp(last_fetch, "last_fetch")
    source_path = f"capabilities/{marker_data['capability_id']}"
    try:
        catalog_source = inspect_git_source(
            hub_root, catalog_commit, source_path, pilot_policy=True
        )
    except CapabilityError as exc:
        return {
            **result,
            "status": "SOURCE_UNAVAILABLE",
            "reason": f"revocation catalog unavailable: {exc}",
            "catalog_commit": catalog_commit,
            "revocation_last_fetch": last_fetch,
        }
    manifest = catalog_source.capability_manifest or {}
    installed_version = marker_data["capability_version"]
    if manifest.get("capability_version") != installed_version:
        return {
            **result,
            "status": "STALE_VIEW",
            "reason": "catalog no longer carries an explicit record for the installed version",
            "catalog_commit": catalog_commit,
            "revocation_last_fetch": last_fetch,
        }
    revoked_at = manifest.get("revoked_at")
    if revoked_at:
        return {
            **result,
            "status": "REVOKED",
            "reason": f"installed release was revoked at {revoked_at}",
            "catalog_commit": catalog_commit,
            "revocation_last_fetch": last_fetch,
            "revoked_at": revoked_at,
        }
    if (
        catalog_source.source_tree != marker_data.get("source_tree")
        or catalog_source.payload_fingerprint
        != marker_data.get("payload_fingerprint")
        or catalog_source.manifest_fingerprint
        != marker_data.get("manifest_fingerprint")
    ):
        return {
            **result,
            "status": "STALE_VIEW",
            "reason": "catalog reuses the installed version with different release bytes",
            "catalog_commit": catalog_commit,
            "revocation_last_fetch": last_fetch,
            "catalog_source_tree": catalog_source.source_tree,
            "catalog_payload_fingerprint": catalog_source.payload_fingerprint,
            "catalog_manifest_fingerprint": catalog_source.manifest_fingerprint,
        }
    return {
        **result,
        "status": "CURRENT",
        "reason": "payload, release source and revocation catalog agree",
        "catalog_commit": catalog_commit,
        "revocation_last_fetch": last_fetch,
    }


def status_all_managed(
    *,
    tools: list[str],
    hub_root: Path | None,
    catalog_commit: str | None,
    last_fetch: str | None,
) -> dict[str, object]:
    roots = local_scan_roots()
    unknown = sorted(set(tools) - set(roots))
    if unknown:
        raise CapabilityError(f"Unsupported local tools: {', '.join(unknown)}")
    if catalog_commit is not None and not GIT_OID.fullmatch(catalog_commit):
        raise CapabilityError("catalog-commit must be a full Git object ID")
    if last_fetch is not None:
        parse_iso_timestamp(last_fetch, "last_fetch")

    results: list[dict[str, object]] = []
    scan_roots: list[dict[str, object]] = []
    for tool in tools:
        spec = roots[tool]
        raw_root = Path(spec["root"])
        try:
            root = absolute_plain_path(raw_root, must_exist=True)
        except CapabilityError as exc:
            scan_roots.append(
                {
                    "tool": tool,
                    "root": str(raw_root.resolve(strict=False)),
                    "status": "UNSUPPORTED",
                    "reason": str(exc),
                }
            )
            continue
        scan_roots.append({"tool": tool, "root": str(root), "status": "SCANNED"})
        try:
            children: list[Path] = []
            with os.scandir(root) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                target = Path(entry.path)
                is_junction = bool(
                    getattr(os.path, "isjunction", lambda _: False)(entry.path)
                )
                if entry.is_symlink() or is_junction:
                    results.append(
                        {
                            "tool": tool,
                            "target": str(target),
                            "status": "UNSUPPORTED",
                            "reason": "scan-root child is a symlink or junction",
                            "writes_performed": 0,
                            "security_statement": SECURITY_STATEMENT,
                        }
                    )
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    children.append(target)
        except OSError as exc:
            scan_roots[-1] = {
                "tool": tool,
                "root": str(root),
                "status": "UNSUPPORTED",
                "reason": str(exc),
            }
            continue
        for target in children:
            try:
                marker_path = find_managed_marker(target)
            except CapabilityError:
                marker_path = next(
                    (target / name for name in MARKER_NAMES if (target / name).exists()),
                    target / CAPABILITY_MARKER_NAME,
                )
            if marker_path is None:
                continue
            marker_data: dict[str, object] | None = None
            marker_is_capability_v2 = False
            repo: Path | None = None
            commit: str | None = None
            source_path: str | None = None
            try:
                marker = read_marker(marker_path)
                marker_data = marker.data
                marker_is_capability_v2 = marker.variant == "v2"
                if marker_is_capability_v2 and hub_root is not None:
                    repo = hub_root
                    commit = str(marker.data["source_commit"])
                    source_path = f"capabilities/{marker.data['capability_id']}"
            except CapabilityError:
                pass
            result = verify_target(
                target,
                tool,
                repo=repo,
                commit=commit,
                source_path=source_path,
                activation_boundary=spec["activation_boundary"],
                last_fetch=last_fetch if catalog_commit else None,
            )
            if marker_data is not None and marker_is_capability_v2:
                result = _catalog_revocation(
                    result,
                    marker_data,
                    hub_root=hub_root,
                    catalog_commit=catalog_commit,
                    last_fetch=last_fetch,
                )
            if ".backup-" in target.name.casefold():
                result["discovery_risk"] = "managed marker exists in a backup-like directory"
                result["severity"] = "HIGH"
            results.append(result)

    counts: dict[str, int] = {}
    for result in results:
        key = str(result.get("status", "UNKNOWN"))
        counts[key] = counts.get(key, 0) + 1
    return {
        "action": "status",
        "mode": "all-managed",
        "writes_performed": 0,
        "security_statement": SECURITY_STATEMENT,
        "hub_root": str(hub_root) if hub_root else None,
        "catalog_commit": catalog_commit,
        "last_fetch": last_fetch,
        "scan_roots": scan_roots,
        "managed_target_count": len(results),
        "status_counts": counts,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "source-inspect":
            result = inspect_git_source(
                args.repo,
                args.commit,
                args.source_path,
                pilot_policy=args.policy == "pilot",
            ).report()
        elif args.action == "materialize":
            if not GIT_OID.fullmatch(args.commit):
                raise CapabilityError(
                    "materialize requires a full owner-approved Git commit OID, not a symbolic revision"
                )
            source = inspect_git_source(
                args.repo, args.commit, args.source_path, pilot_policy=True
            )
            result = materialize_git_source(source, args.output)
        elif args.action == "marker-check":
            result = read_marker(args.marker).report()
        elif args.action == "scan":
            result = scan_directory(args.target)
        elif args.action == "verify":
            result = verify_target(
                args.target,
                args.tool,
                repo=args.repo,
                commit=args.commit,
                source_path=args.source_path,
                activation_boundary=args.activation_boundary,
                last_fetch=args.last_fetch,
            )
        elif args.action == "verify-local":
            hub_root = Path(args.hub_root).resolve(strict=True)
            specs = local_specs(hub_root)
            selected = [item.strip() for item in args.tools.split(",") if item.strip()]
            unknown = sorted(set(selected) - set(specs))
            if unknown:
                raise CapabilityError(f"Unsupported local tools: {', '.join(unknown)}")
            result = {
                "action": "verify-local",
                "hub_root": str(hub_root),
                "results": [
                    verify_target(
                        specs[tool]["target"],
                        tool,
                        repo=hub_root,
                        commit=args.commit,
                        source_path=specs[tool]["source_path"],
                        activation_boundary=specs[tool]["activation_boundary"],
                    )
                    for tool in selected
                ],
            }
        elif args.action == "status":
            if not args.all_managed:
                raise CapabilityError("status currently requires --all-managed")
            selected = [item.strip() for item in args.tools.split(",") if item.strip()]
            hub_root = Path(args.hub_root).resolve(strict=True) if args.hub_root else None
            result = status_all_managed(
                tools=selected,
                hub_root=hub_root,
                catalog_commit=args.catalog_commit,
                last_fetch=args.last_fetch,
            )
        else:
            raise CapabilityError(f"Unsupported action: {args.action}")
        emit(result)
        if args.action == "status":
            critical = {
                "DRIFT",
                "MALFORMED_MARKER",
                "REVOKED",
                "SOURCE_UNAVAILABLE",
                "STALE_VIEW",
            }
            root_failed = any(
                item.get("status") != "SCANNED" for item in result["scan_roots"]
            )
            if root_failed or any(
                item.get("status") in critical or item.get("severity") == "HIGH"
                for item in result["results"]
            ):
                return 3
        return 0
    except CapabilityError as exc:
        read_only = args.action in {
            "source-inspect",
            "marker-check",
            "scan",
            "verify",
            "verify-local",
            "status",
        }
        emit(
            {
                "status": "ERROR",
                "error": str(exc),
                "writes_performed": 0 if read_only else "TEMP_ONLY_OR_NONE",
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
