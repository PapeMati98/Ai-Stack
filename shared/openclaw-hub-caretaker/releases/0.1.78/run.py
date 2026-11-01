"""hub-caretaker CLI entry point.

Run-once; no scheduler. Three modes:

- ``--dry-run``   print the planned pipeline and exit.
- ``--status``    print the SQLite durable summary and exit.
- ``--once``      run the full pipeline once with the configured argv
                  transports (default).

Exit codes:

- ``0``  success or no-op (no_input / pending_handoffs_drained).
- ``1``  pipeline ran but raised an outage / hub_offline / dirty_tree
         / unsupported_input incident; the durable queue still has work
         pending for the next run.
- ``2``  configuration / startup error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Make ``caretaker`` importable when invoked as ``python run.py``.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from caretaker.config import CaretakerConfig, ConfigError, load_config
from caretaker.hub import CanonicalKnowledgeGateway, KnowledgeGateway
from caretaker.office import (CanonicalOfficeArgvTransport, OfficeConnector,
                             OfficeDedupStore, OfficeIdentity, RateLimiter)
from caretaker.pipeline import PipelineResult, build_default_deps, \
    run_pipeline
from caretaker.status import dry_run_plan, status_report
from caretaker.store import Store
from caretaker.transports import (FakeTransport, OpenClawArgvTransport,
                                  Transport, TransportError, Envelope)


# ``--once`` exits non-zero whenever an outage or hub-side incident is
# raised.  The durable outbox is left untouched so a subsequent run can
# resume.  Operator-visible notes are emitted as JSON for parsing.
_FATAL_INCIDENT_KINDS = (
    "hub_health", "hub_lint", "hub_offline", "context_stale",
    "dirty_worktree", "local_outage", "merge_target_missing",
    "merge_target_invalid", "unresolved_citations", "invalid_decision",
    "duplicate_without_evidence",
)
_FATAL_NOTES = (
    "hub_preflight_failed", "context_verification_failed",
    "context_not_verified", "handoff_note_failed",
    "duplicate_without_evidence",
)


def _pipeline_exit_code(result: PipelineResult) -> int:
    """Map a ``PipelineResult`` to the run-once process exit code."""
    if result.local_outage:
        return 1
    for incident in result.incidents:
        if any(incident.startswith(kind) for kind in _FATAL_INCIDENT_KINDS):
            return 1
    if any(note in _FATAL_NOTES for note in (result.notes or "").split(",")):
        return 1
    return 0


def _build_store(config: CaretakerConfig) -> Store:
    db_path = Path(config.state_dir) / "caretaker.sqlite"
    return Store(str(db_path))


def _build_gateway(config: CaretakerConfig) -> KnowledgeGateway:
    receipt_path = Path(config.hub.context_receipt)
    receipt_root = str(receipt_path.parent) if receipt_path.name else str(receipt_path)
    return CanonicalKnowledgeGateway(
        python_executable=config.hub.python_executable,
        script=config.hub.script,
        vault=config.hub.vault,
        receipt_root=receipt_root,
    )


def _build_office(config: CaretakerConfig) -> OfficeConnector:
    identity = OfficeIdentity(
        harness=config.office.harness,
        host=config.office.host,
        session_id=config.office.session_id,
    )
    return OfficeConnector(
        identity=identity,
        transport=CanonicalOfficeArgvTransport(
            executable=config.office.wrapper or config.office.script,
            home=config.office.home,
            harness=config.office.harness,
            session_id=config.office.session_id,
            python_executable=config.office.python_executable,
            script=config.office.script,
        ),
        dedup=OfficeDedupStore(
            path=str(Path(config.state_dir) / "office-dedup.json")
            if config.office.dedup else None,
        ),
        rate=RateLimiter(
            max_per_minute=config.office.rate_per_minute,
            path=str(Path(config.state_dir) / "office-rate.json"),
        ),
        no_autoplay_reply=config.office.no_autoplay_reply,
    )


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hub-caretaker",
        description="Run-once hub caretaker integration (no scheduler).",
    )
    parser.add_argument("--config", required=True,
                        help="Path to caretaker config JSON.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Print the planned pipeline and exit.")
    mode.add_argument("--status", action="store_true",
                      help="Print the durable store summary and exit.")
    mode.add_argument("--once", action="store_true",
                      help="Run the full pipeline once (default).")
    parser.add_argument("--worktree-root", default=os.getcwd(),
                        help="Workspace root for the dirty-tree check.")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        _emit(dry_run_plan(config))
        return 0

    store = _build_store(config)
    try:
        if args.status:
            _emit(status_report(store))
            return 0
        deps = build_default_deps(
            config, _build_gateway(config), worktree_root=args.worktree_root,
            office_override=_build_office(config),
        )
        result: PipelineResult = run_pipeline(
            config, deps, store=store, dry_run=False,
        )
        _emit({
            "run_id": result.run_id,
            "dry_run": result.dry_run,
            "records_read": result.records_read,
            "records_admitted": result.records_admitted,
            "proposals_new": result.proposals_new,
            "proposals_duplicate": result.proposals_duplicate,
            "proposals_merge": result.proposals_merge,
            "proposals_contradiction": result.proposals_contradiction,
            "local_outage": result.local_outage,
            "fallback_used": result.fallback_used,
            "incidents": list(result.incidents),
            "notes": result.notes,
            "model_identity": result.model_identity,
            "exit_code": _pipeline_exit_code(result),
        })
        # ``--once`` reports failures through the process exit code so the
        # operator can wire it into a scheduler.  The durable outbox is
        # kept pending for the next run; this exit code only signals
        # "no successful completion this time", never "delete the queue".
        return _pipeline_exit_code(result)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
