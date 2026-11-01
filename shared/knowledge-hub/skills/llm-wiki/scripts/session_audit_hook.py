"""Best-effort metadata hook for the canonical Agent Stack audit producer."""

from __future__ import annotations

import copy
import importlib.abc
import importlib.util
import sys
from pathlib import Path
from typing import Any


_SIBLING_MODULES = frozenset({"audit_core", "completion"})
_PRODUCER_MODULE = "_codex_session_audit_hook_producer"
_MISSING = object()


def _canonical_producer_script() -> Path:
    """Return the installed producer path; runtime redirects are not honored."""
    return (
        Path.home()
        / ".agents"
        / "skills"
        / "delivery-audit"
        / "scripts"
        / "producer.py"
    )


class _TrustedSiblingFinder(importlib.abc.MetaPathFinder):
    """Resolve producer-owned absolute imports only from its installed folder."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _SIBLING_MODULES:
            return None

        sibling = self.directory / f"{fullname}.py"
        if not sibling.is_file():
            raise ModuleNotFoundError(f"Missing trusted producer sibling: {fullname}")
        return importlib.util.spec_from_file_location(fullname, sibling)


def _call_canonical_producer(
    receipt_snapshot: dict[str, Any], args: Any, project_slug: str
) -> None:
    """Load the trusted producer and its siblings, then invoke it once."""
    script = _canonical_producer_script()
    directory = script.parent
    sibling_finder = _TrustedSiblingFinder(directory)
    managed_names = (*_SIBLING_MODULES, _PRODUCER_MODULE)
    previous_modules = {name: sys.modules.get(name, _MISSING) for name in managed_names}
    previous_path = sys.path[:]
    previous_meta_path = sys.meta_path[:]
    previous_bytecode = sys.dont_write_bytecode

    try:
        sys.dont_write_bytecode = True
        for name in managed_names:
            sys.modules.pop(name, None)

        sys.path.insert(0, str(directory))
        sys.meta_path.insert(0, sibling_finder)

        spec = importlib.util.spec_from_file_location(_PRODUCER_MODULE, script)
        if spec is None or spec.loader is None:
            raise ImportError("Unable to load canonical audit producer")

        producer = importlib.util.module_from_spec(spec)
        sys.modules[_PRODUCER_MODULE] = producer
        spec.loader.exec_module(producer)
        producer.after_hub_closeout(
            receipt_snapshot,
            args.agent,
            args.session_id,
            project_slug,
            checkpoint_id=args.checkpoint_id,
        )
    finally:
        sys.dont_write_bytecode = previous_bytecode
        sys.path[:] = previous_path
        sys.meta_path[:] = previous_meta_path
        for name, previous in previous_modules.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def after_record_session(final_receipt: Any, args: Any) -> Any:
    """Run the audit producer after a final completed Hub closeout, fail-open.

    The original receipt is returned unchanged. Producer failures are deliberately
    isolated from the product closeout path.
    """
    if not isinstance(final_receipt, dict):
        return final_receipt
    if (
        final_receipt.get("action") != "record-session"
        or final_receipt.get("receipt_status") != "COMPLETE"
        or final_receipt.get("outcome") != "COMPLETED"
    ):
        return final_receipt

    project_slug = final_receipt.get("project_slug")
    if not isinstance(project_slug, str):
        return final_receipt

    try:
        snapshot = copy.deepcopy(final_receipt)
        _call_canonical_producer(snapshot, args, project_slug)
    except Exception:
        # Audit dispatch is best-effort. Do not leak producer errors into the receipt.
        pass
    return final_receipt
