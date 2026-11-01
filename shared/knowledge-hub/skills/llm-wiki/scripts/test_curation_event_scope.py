"""Synthetic checks for scoped event snapshots inside the publish fence."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import test_curation

curation = test_curation.curation


class CurationEventScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.a = self.vault / "events" / "2026" / "10" / "a"
        self.b = self.vault / "events" / "2026" / "10" / "b"
        self.a.mkdir(parents=True)
        self.b.mkdir(parents=True)
        self.paths = []
        for index in range(6):
            parent = self.a if index % 2 == 0 else self.b
            path = parent / f"evt-{index:04d}.json"
            path.write_bytes(f"synthetic-event-{index}".encode())
            self.paths.append(path)

    def test_grouped_snapshot_preserves_bytes_and_path_order_with_one_walk_per_parent(self):
        expected = {path.relative_to(self.vault).as_posix(): path.read_bytes()
                    for path in self.paths}
        walks = []
        scopes = []
        rechecks = []
        absolute_paths = []
        scope_lock = threading.Lock()
        real_walk = curation.knowledge._walk_reparse_components
        real_scope = curation.knowledge.invocation_read_scope
        real_recheck = curation.knowledge._recheck_invocation_ancestors
        real_absolute = curation._absolute
        real_read = curation._read

        @contextmanager
        def capture_scope(parent):
            with real_scope(parent) as state:
                with scope_lock:
                    scopes.append((parent, state))
                yield state

        def count_walk(root, path, scope):
            walks.append((root, path))
            return real_walk(root, path, scope)

        def count_recheck():
            rechecks.append(threading.get_ident())
            return real_recheck()

        def count_absolute(path):
            if path in self.paths:
                absolute_paths.append(path)
            return real_absolute(path)

        def scoped_read(path):
            self.assertIsNotNone(curation.knowledge._scope_for_reparse(path.parent))
            return real_read(path)

        with mock.patch.object(curation, "_tree_files", return_value=self.paths), \
                mock.patch.object(curation.knowledge, "_walk_reparse_components",
                                  side_effect=count_walk), \
                mock.patch.object(curation.knowledge, "invocation_read_scope",
                                  side_effect=capture_scope), \
                mock.patch.object(curation.knowledge, "_recheck_invocation_ancestors",
                                  side_effect=count_recheck), \
                mock.patch.object(curation, "_absolute", side_effect=count_absolute), \
                mock.patch.object(curation, "_read", side_effect=scoped_read):
            snapshot = curation._event_snapshot(self.vault)

        self.assertEqual(snapshot, expected)
        self.assertEqual(list(snapshot), list(expected))
        self.assertEqual(len(walks), 2)
        self.assertEqual(len(scopes), 2)
        self.assertEqual({parent for parent, _state in scopes}, {self.a, self.b})
        self.assertEqual(len({id(state) for _parent, state in scopes}), 2)
        self.assertEqual(len(rechecks), 2)
        self.assertCountEqual(absolute_paths, self.paths)

    def test_grouped_workers_keep_concurrency_bounded(self):
        paths = []
        for index in range(8):
            parent = self.vault / "events" / "2026" / "11" / f"group-{index:02d}"
            parent.mkdir(parents=True)
            path = parent / f"evt-{index:04d}.json"
            path.write_bytes(f"concurrent-event-{index}".encode())
            paths.append(path)
        active = 0
        maximum = 0
        guard = threading.Lock()
        real_read = curation._read

        def slow_read(path):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.02)
                return real_read(path)
            finally:
                with guard:
                    active -= 1

        with mock.patch.object(curation, "_tree_files", return_value=paths), \
                mock.patch.object(curation, "_read", side_effect=slow_read):
            result = curation._event_snapshot(self.vault)

        self.assertEqual(len(result), len(paths))
        self.assertGreaterEqual(maximum, 2)
        self.assertLessEqual(maximum, curation.IO_WORKERS)

    def test_late_reparse_at_group_recheck_fails_closed(self):
        late = {"enabled": False}
        real_component = curation.knowledge._component_is_reparse
        real_recheck = curation.knowledge._recheck_invocation_ancestors

        def introduce_late_reparse(path, stat_result):
            if late["enabled"] and path == self.a:
                return True
            return real_component(path, stat_result)

        def mark_late_reparse():
            late["enabled"] = True
            return real_recheck()

        with mock.patch.object(curation, "_tree_files", return_value=self.paths), \
                mock.patch.object(curation.knowledge, "_component_is_reparse",
                                  side_effect=introduce_late_reparse), \
                mock.patch.object(curation.knowledge, "_recheck_invocation_ancestors",
                                  side_effect=mark_late_reparse):
            with self.assertRaisesRegex(curation.CurationError, "reparse component"):
                curation._event_snapshot(self.vault)

    def test_file_mutation_during_secure_read_fails_closed(self):
        target = self.paths[0]
        original = target.read_bytes()
        replacement = b"x" * len(original)
        real_read = curation.knowledge.os.read
        mutated = False

        def mutate_after_read(descriptor, size):
            nonlocal mutated
            chunk = real_read(descriptor, size)
            if chunk and not mutated:
                target.write_bytes(replacement)
                mutated = True
            return chunk

        with mock.patch.object(curation, "_tree_files", return_value=[target]), \
                mock.patch.object(curation.knowledge.os, "read", side_effect=mutate_after_read):
            with self.assertRaisesRegex(curation.CurationError, "changed during secure read"):
                curation._event_snapshot(self.vault)

        self.assertTrue(mutated)
        target.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
