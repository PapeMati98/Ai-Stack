"""Package payload checks for the ZCode Desktop external-CU transport.

Validates the exact installer/runtime mapping and package.json bytes for the
new host and typed-guard modules. Does not deploy, schedule, or contact a live
Office store.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import install_agent_office as installer  # noqa: E402

ZCODE_PAYLOAD = (
    "coordfs/zcode_desktop_tool_host.py",
    "coordfs/desktop_adapters.py",
)


def test_zcode_payload_declared_in_runtime_source_map() -> None:
    declared = dict(installer.RUNTIME_SOURCE_MAP)
    for rel in ZCODE_PAYLOAD:
        assert declared.get(rel) == rel
    assert declared.get("coordfs/leases.py") == "coordfs/leases.py"
    assert declared.get("coordfs/codex_desktop_tool_host.py") == "coordfs/codex_desktop_tool_host.py"


def test_package_manifest_contains_exact_zcode_payload_and_bytes() -> None:
    # seal_package is the reviewed build step that refreshes digests from the
    # declared sources without touching any deployment.
    sealed = installer.seal_package()
    assert sealed["status"] in {"READY", "CURRENT"}
    package = installer.verify_package()
    for rel in ZCODE_PAYLOAD:
        source = installer.SOURCE_ROOT / rel
        data = source.read_bytes()
        assert package.runtime_files[rel] == data
        digest = hashlib.sha256(data).hexdigest()
        row = next(
            item for item in package.manifest["runtime"] if item.get("source") == rel
        )
        assert row["target"] == rel
        assert row["sha256"] == digest
        assert row["bytes"] == len(data)
    leases_row = next(
        item for item in package.manifest["runtime"] if item.get("source") == "coordfs/leases.py"
    )
    leases_bytes = (installer.SOURCE_ROOT / "coordfs" / "leases.py").read_bytes()
    assert leases_row["sha256"] == hashlib.sha256(leases_bytes).hexdigest()
    assert leases_row["bytes"] == len(leases_bytes)


def test_existing_codex_desktop_rows_remain_declared() -> None:
    declared = dict(installer.RUNTIME_SOURCE_MAP)
    assert declared.get("coordfs/codex_desktop_tool_host.py") == "coordfs/codex_desktop_tool_host.py"
    manifest = json.loads((installer.PACKAGE_PATH).read_text(encoding="utf-8-sig"))
    sources = {row.get("source") for row in manifest.get("runtime", [])}
    assert "coordfs/codex_desktop_tool_host.py" in sources
    assert "host/codex_app_transport.js" in sources
