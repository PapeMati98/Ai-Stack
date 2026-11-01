"""The deployed runtime must expose the deterministic lease CLI.

Measured on 2026-09-20: the installed ``~/.agents/runtime/agent-office/`` held
only ``office.cmd``, ``office.py`` and a truncated ``coordfs`` package - no
``coord.cmd``, no ``coord.py``, no ``coordfs/cli.py``, no ``coordfs/doctor.py``
and no ``coordfs/__main__.py``. The lease mechanism existed in code and was
tested, but nothing deployed could invoke it, so the only model-free and
token-free anti-collision path in the system was unreachable.

These tests pin that closure end to end: the five payload entries the installer
must ship, the manifest rows they produce, the dry-run planned runtime tree, and
an import of ``coordfs.cli`` restricted to exactly the files in
``RUNTIME_SOURCE_MAP``. Nothing here writes, deploys, or removes anything - the
dry-run case stubs ``normalize_layout`` so it plans against a synthetic home
that is never created, and the restricted import restores ``sys.modules``.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install_agent_office as installer

from coordfs.leases import CoordBoard

from support import CoordTestCase


# coord.cmd -> coord.py -> coordfs.cli -> {constants, doctor, errors, identity,
# leases}; coordfs.doctor -> {constants, leases, store}. The rest of the closure
# was already shipped; these five were the missing files.
LEASE_CLI_ENTRIES: tuple[tuple[str, str], ...] = (
    ("coord.cmd", "coord.cmd"),
    ("coord.py", "coord.py"),
    ("coordfs/__main__.py", "coordfs/__main__.py"),
    ("coordfs/cli.py", "coordfs/cli.py"),
    ("coordfs/doctor.py", "coordfs/doctor.py"),
)


def payload_modules() -> tuple[dict[str, Path], set[str]]:
    """Every coordfs module in the payload: module name -> source file."""
    paths: dict[str, Path] = {}
    packages: set[str] = set()
    for source, target in installer.RUNTIME_SOURCE_MAP:
        if not target.startswith("coordfs/") or not target.endswith(".py"):
            continue
        stem = target[: -len(".py")]
        if stem.endswith("/__init__"):
            name = stem[: -len("/__init__")].replace("/", ".")
            packages.add(name)
        else:
            name = stem.replace("/", ".")
        paths[name] = installer.SOURCE_ROOT / source
    return paths, packages


def imported_coordfs_names(module_name: str, path: Path, packages: set[str]) -> set[str]:
    """coordfs module names a module imports, including deferred ones."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = module_name.split(".")
    anchor = parts if module_name in packages else parts[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.split(".")[0] == "coordfs")
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            prefix = anchor[: len(anchor) - (node.level - 1)]
            if node.module:
                names.add(".".join([*prefix, node.module]))
            else:
                names.update(".".join([*prefix, alias.name]) for alias in node.names)
        elif node.module and node.module.split(".")[0] == "coordfs":
            names.add(node.module)
    return names


def payload_import_closure(entry: str) -> tuple[set[str], list[str]]:
    """Modules reachable from ``entry``, plus imports with no payload file.

    Importing a submodule also executes its parent packages, so every ancestor
    package is seeded as a requirement of the entry.
    """
    paths, packages = payload_modules()
    reached: set[str] = set()
    broken: list[str] = []
    seen: set[str] = set()
    parts = entry.split(".")
    queue: list[tuple[str, str]] = [
        (entry, ".".join(parts[:index])) for index in range(1, len(parts) + 1)
    ]
    while queue:
        importer, name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        source_path = paths.get(name)
        if source_path is None:
            broken.append(f"{importer} imports {name}, which is not in RUNTIME_SOURCE_MAP")
            continue
        reached.add(name)
        for child in sorted(imported_coordfs_names(name, source_path, packages)):
            queue.append((name, child))
    return reached, broken


class _PayloadOnlyFinder:
    """Serve coordfs modules from the payload only; refuse anything else."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = paths

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname != "coordfs" and not fullname.startswith("coordfs."):
            return None
        location = self._paths.get(fullname)
        if location is None:
            raise ImportError(
                f"{fullname} is not in RUNTIME_SOURCE_MAP: the deployed runtime would not have it"
            )
        return importlib.util.spec_from_file_location(fullname, location)


def import_with_payload_only(entry: str):
    """Import ``entry`` with coordfs served only from the installer payload."""
    paths, _ = payload_modules()
    finder = _PayloadOnlyFinder(paths)
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "coordfs" or name.startswith("coordfs.")
    }
    for name in saved:
        del sys.modules[name]
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, finder)
    try:
        return importlib.import_module(entry)
    finally:
        sys.meta_path.remove(finder)
        for name in [
            name for name in sys.modules if name == "coordfs" or name.startswith("coordfs.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)
        sys.dont_write_bytecode = previous_bytecode


class RuntimeSourceMapTests(unittest.TestCase):
    """The payload list is the contract; the manifest is derived from it."""

    def test_runtime_source_map_ships_the_lease_cli_entries(self) -> None:
        for pair in LEASE_CLI_ENTRIES:
            self.assertIn(pair, installer.RUNTIME_SOURCE_MAP)
        sources = [source for source, _ in installer.RUNTIME_SOURCE_MAP]
        targets = [target for _, target in installer.RUNTIME_SOURCE_MAP]
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual(len(targets), len(set(targets)))

    def test_coord_cmd_starts_the_entry_deployed_next_to_it(self) -> None:
        text = (installer.SOURCE_ROOT / "coord.cmd").read_text(encoding="utf-8")
        self.assertIn("%~dp0coord.py", text)
        self.assertNotIn(":\\", text.replace("%~dp0", ""))
        self.assertIn(("coord.cmd", "coord.cmd"), installer.RUNTIME_SOURCE_MAP)


class SealedManifestTests(unittest.TestCase):
    """`python install_agent_office.py seal` is the gate that refreshes these rows."""

    def test_manifest_rows_equal_the_map_and_verify_package_accepts_them(self) -> None:
        manifest = json.loads(installer.PACKAGE_PATH.read_text(encoding="utf-8-sig"))
        self.assertEqual(
            [(row["source"], row["target"]) for row in manifest["runtime"]],
            [pair for pair in installer.RUNTIME_SOURCE_MAP],
        )
        package = installer.verify_package()
        for _, target in LEASE_CLI_ENTRIES:
            self.assertIn(target, package.runtime_files)


class DryRunPayloadTests(unittest.TestCase):
    """A write-free dry run must plan coord.cmd into the runtime tree.

    ``normalize_layout`` is stubbed so the planner runs against a synthetic home
    that is never created: dry-run performs no writes, and this test must not
    create or remove anything either.
    """

    def setUp(self) -> None:
        self.home = Path(tempfile.gettempdir()) / "agent-office-synthetic-dry-run-home"
        self.appdata = self.home / "AppData" / "Roaming"
        self.state = self.home / ".agents" / "agent-office-deploy"
        self.python = self.home / "Python Runtime With Spaces" / "python.exe"
        self.pin_patch = mock.patch.object(installer, "pin_python", return_value=self.python)
        self.pin_patch.start()
        self.layout_patch = mock.patch.object(
            installer, "normalize_layout", return_value=(self.home, self.appdata, self.state)
        )
        self.layout_patch.start()

    def tearDown(self) -> None:
        self.layout_patch.stop()
        self.pin_patch.stop()

    def test_dry_run_plans_the_lease_cli_into_the_runtime_payload(self) -> None:
        result = installer.dry_run(self.home, self.appdata, self.state, self.python)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["writes_performed"], 0)
        planned = next(row for row in result["targets"] if row["id"] == "runtime")

        specs = installer.build_install_specs(
            installer.verify_package(), self.home, self.python, "0" * 32
        )
        desired = next(spec for spec in specs if spec["id"] == "runtime")["desired"]
        for _, target in LEASE_CLI_ENTRIES:
            self.assertIn(target, desired)
        self.assertIn("office.cmd", desired)
        self.assertEqual(planned["planned"], installer.virtual_tree_snapshot(desired))


class LeaseCliImportClosureTests(unittest.TestCase):
    """coordfs.cli must import from the payload alone - the closure falsifier."""

    def test_every_coordfs_import_of_the_cli_is_in_the_payload(self) -> None:
        reached, broken = payload_import_closure("coordfs.cli")
        self.assertEqual(broken, [])
        for name in (
            "coordfs",
            "coordfs.cli",
            "coordfs.doctor",
            "coordfs.leases",
            "coordfs.store",
            "coordfs.constants",
            "coordfs.errors",
            "coordfs.identity",
        ):
            self.assertIn(name, reached)

    def test_coordfs_cli_main_imports_with_only_the_payload_modules(self) -> None:
        module = import_with_payload_only("coordfs.cli")
        self.assertTrue(callable(module.main))
        self.assertTrue(callable(module.build_parser))
        self.assertTrue(callable(module.dispatch))

    def test_coordfs_main_module_imports_with_only_the_payload_modules(self) -> None:
        module = import_with_payload_only("coordfs.__main__")
        self.assertTrue(callable(module.main))


class NotHolderPayloadTests(CoordTestCase):
    """release/renew from a non-holder must return ``not_holder`` with the caller
    identity strength and a hint pointing at the session-id machinery.

    ``claim`` is unchanged: its non-holder branch keeps returning ``held`` with
    ``status=warning`` so existing consumers (the warning payload schema, exit
    code derivation, the held-shape parser) keep matching on the same fields.
    These tests pin the not_holder payload shape and pin that the lock is not
    unlinked by a non-holder.
    """

    def test_release_by_non_holder_returns_not_holder_with_caller_strength(self) -> None:
        claimed = self.board.claim("data:payload-release", "holder-agent", ttl_s=60)
        self.assertEqual(claimed["status"], "ok")
        blocked = self.board.release("data:payload-release", "intruder-agent", caller_strength="weak")
        self.assertEqual(blocked["status"], "warning")
        self.assertEqual(blocked["code"], "not_holder")
        self.assertEqual(blocked["holder_agent"], claimed["holder_agent"])
        self.assertEqual(blocked["caller_agent_id"], "intruder-agent")
        self.assertEqual(blocked["caller_strength"], "weak")
        for key in ("ttl_remaining_s", "ttl_hint_s", "expires_at", "hint"):
            self.assertIn(key, blocked)
        self.assertIn("--session-id", blocked["hint"])
        self.assertIn("COORDFS_SESSION_ID", blocked["hint"])
        self.assertEqual(blocked["hint"], blocked["message"])

    def test_renew_by_non_holder_returns_not_holder_with_caller_strength(self) -> None:
        claimed = self.board.claim("data:payload-renew", "holder-agent", ttl_s=60)
        self.assertEqual(claimed["status"], "ok")
        blocked = self.board.renew("data:payload-renew", "intruder-agent", ttl_s=90, caller_strength="strong")
        self.assertEqual(blocked["status"], "warning")
        self.assertEqual(blocked["code"], "not_holder")
        self.assertEqual(blocked["holder_agent"], claimed["holder_agent"])
        self.assertEqual(blocked["caller_strength"], "strong")
        self.assertIn("--session-id", blocked["message"])

    def test_release_by_holder_still_returns_released_without_caller_payload(self) -> None:
        claimed = self.board.claim("data:payload-released", "holder-agent", ttl_s=60)
        self.assertEqual(claimed["status"], "ok")
        released = self.board.release("data:payload-released", "holder-agent", caller_strength="strong")
        self.assertEqual(released["status"], "ok")
        self.assertEqual(released["code"], "released")
        self.assertNotIn("caller_agent_id", released)
        self.assertNotIn("caller_strength", released)
        self.assertNotIn("hint", released)

    def test_claim_warning_still_uses_held_code_and_shape(self) -> None:
        first = self.board.claim("data:payload-claim-held", "holder-agent", ttl_s=60)
        self.assertEqual(first["status"], "ok")
        warned = self.board.claim("data:payload-claim-held", "loser-agent")
        self.assertEqual(warned["status"], "warning")
        self.assertEqual(warned["code"], "held")
        self.assertEqual(warned["holder_agent"], first["holder_agent"])
        self.assertNotIn("caller_agent_id", warned)
        self.assertNotIn("hint", warned)

    def test_release_by_non_holder_does_not_unlink_lease(self) -> None:
        claimed = self.board.claim("data:payload-still-held", "holder-agent", ttl_s=60)
        self.assertEqual(claimed["status"], "ok")
        blocked = self.board.release("data:payload-still-held", "intruder-agent", caller_strength="weak")
        self.assertEqual(blocked["code"], "not_holder")
        who = self.board.who("data:payload-still-held")
        self.assertEqual(who["code"], "who")
        self.assertEqual(who["holder_agent"], claimed["holder_agent"])

    def test_renew_by_non_holder_does_not_advance_acquired_at(self) -> None:
        claimed = self.board.claim("data:payload-renew-intact", "holder-agent", ttl_s=60)
        self.assertEqual(claimed["status"], "ok")
        original_acquired_at = claimed["lease"]["acquired_at"]
        original_ttl = claimed["lease"]["ttl_s"]
        self.advance(5)
        blocked = self.board.renew("data:payload-renew-intact", "intruder-agent", caller_strength="strong")
        self.assertEqual(blocked["code"], "not_holder")
        who = self.board.who("data:payload-renew-intact")
        self.assertEqual(who["lease"]["acquired_at"], original_acquired_at)
        self.assertEqual(who["lease"]["ttl_s"], original_ttl)


if __name__ == "__main__":
    unittest.main()
