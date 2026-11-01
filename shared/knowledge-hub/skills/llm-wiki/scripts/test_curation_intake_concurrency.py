"""Synthetic REAL concurrency: contributor intake vs canonical publication.

Real hidden subprocesses append valid Unicode events through the contributor
gateway (knowledge.write_event) while the canonical publisher stages, applies
and final-lints. No network, no models, no real vault. The writer fence makes
each event land entirely inside or outside a fenced snapshot; append-only
intake must never roll back a publication, while mutation, deletion, or an
invalid new event identity still does.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

import test_curation
from test_curation import sha, write_json

curation = test_curation.curation

WRITER = (
    "import importlib.util, sys, time\n"
    "from pathlib import Path\n"
    "spec = importlib.util.spec_from_file_location('knowledge', sys.argv[1])\n"
    "knowledge = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(knowledge)\n"
    "vault = Path(sys.argv[2])\n"
    "for i in range(int(sys.argv[3])):\n"
    "    knowledge.write_event(vault, 'writer-' + sys.argv[4], 'stage-source',\n"
    "        'Unicode intake ' + sys.argv[4] + ':' + str(i) + ' Zażółć gęślą jaźń 今日')\n"
    "    time.sleep(0.05)\n"
)


def _spawn_writer(knowledge_py: Path, vault: Path, count: int, tag: str):
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return subprocess.Popen(
        [sys.executable, "-B", "-X", "utf8", "-c", WRITER,
         str(knowledge_py), str(vault), str(count), tag],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


class IntakeConcurrencyTests(unittest.TestCase):
    def test_all_intake_finished_during_final_lint_is_in_committed_log(self):
        f = self._fixture()
        real_lint = curation.knowledge.lint
        appended = []
        def append_at_final(path, **kwargs):
            result = real_lint(path, **kwargs)
            if Path(path) == f.vault and (f.vault/'wiki/test.md').exists() and not appended:
                proc = _spawn_writer(Path(curation.knowledge.__file__), f.vault, 4, 'final')
                out, err = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 0, err.decode('utf-8','replace'))
                appended.append(True)
            return result
        with mock.patch.object(curation.knowledge, 'lint', side_effect=append_at_final):
            result = f.publish()
        self.assertEqual(result['status'], 'PUBLISHED')
        log = (f.vault/'log.md').read_text(encoding='utf-8')
        for i in range(4):
            self.assertEqual(log.count(f'Unicode intake final:{i}'), 1)

    def _fixture(self):
        fixture = test_curation.CurationFixture(
            methodName="test_real_lint_publish_rollback_exact_bytes")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_concurrent_unicode_intake_never_rolls_back_or_duplicates(self):
        f = self._fixture()
        vault = f.vault
        knowledge_py = Path(curation.knowledge.__file__)
        writers = [_spawn_writer(knowledge_py, vault, 4, tag) for tag in ("a", "b")]
        original_replace = curation._atomic_replace

        def slow_replace(path, content, **kwargs):
            # Widen the apply window so intake lands mid-publication.
            if Path(path).is_relative_to(vault):
                time.sleep(0.25)
            return original_replace(path, content, **kwargs)

        try:
            with mock.patch.object(curation, "_atomic_replace", side_effect=slow_replace):
                result = f.publish()
            for proc in writers:
                out, err = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        finally:
            for proc in writers:
                if proc.poll() is None:
                    proc.kill()
        self.assertEqual(result["status"], "PUBLISHED")
        written = sorted(vault.glob("events/*/*/evt-*.json"))
        writer_events = [p for p in written
                         if json.loads(p.read_text(encoding="utf-8"))["agent"].startswith("writer-")]
        self.assertEqual(len(writer_events), 8)
        log = (vault / "log.md").read_text(encoding="utf-8")
        included = 0
        for tag in ("a", "b"):
            for i in range(4):
                # exactly-once: merged at most once, never duplicated or dropped mid-write
                count = log.count(f"Unicode intake {tag}:{i}")
                self.assertLessEqual(count, 1)
                included += count
        self.assertGreater(included, 0)
        self.assertEqual(curation.knowledge.lint(vault)["error_count"], 0)

    def test_preexisting_event_mutation_during_apply_still_rolls_back(self):
        f = self._fixture()
        event = f.vault / "events/2026/09/evt-existing.json"
        mutated = event.read_bytes().replace(b"Earlier evidence", b"Changed during apply")
        original = curation._write_journal

        def mutate_during_apply(path, journal):
            original(path, journal)
            if journal["status"] == "applying" and event.read_bytes() != mutated:
                event.write_bytes(mutated)

        with mock.patch.object(curation, "_write_journal", side_effect=mutate_during_apply):
            with self.assertRaisesRegex(curation.CurationError, "mutated or deleted|baseline changed"):
                f.publish()
        expected = dict(f.before)
        expected["events/2026/09/evt-existing.json"] = mutated
        self.assertEqual(expected, f.vault_bytes())

    def test_preexisting_event_deletion_during_final_lint_rolls_back(self):
        f = self._fixture()
        event = f.vault / "events/2026/09/evt-existing.json"
        real_lint = curation.knowledge.lint
        deleted = []

        def delete_during_final_lint(path, **kwargs):
            outcome = real_lint(path, **kwargs)
            if Path(path) == f.vault and (f.vault / "wiki/test.md").exists() and not deleted:
                deleted.append(True)
                event.unlink()
            return outcome

        with mock.patch.object(curation.knowledge, "lint", side_effect=delete_during_final_lint):
            with self.assertRaisesRegex(curation.CurationError, "mutated or deleted|baseline changed"):
                f.publish()
        self.assertTrue(deleted)
        expected = dict(f.before)
        del expected["events/2026/09/evt-existing.json"]
        self.assertEqual(expected, f.vault_bytes())

    def test_invalid_new_event_identity_during_apply_rolls_back(self):
        f = self._fixture()
        forged = f.vault / "events/2026/09/evt-forged-name.json"
        original = curation._write_journal

        def forge_during_apply(path, journal):
            original(path, journal)
            if journal["status"] == "applying" and not forged.exists():
                write_json(forged, {
                    "event_id": "evt-DIFFERENT", "occurred_at": "2026-09-11T00:00:00Z",
                    "agent": "other", "action": "stage-source", "summary": "Forged identity",
                    "source_ids": [], "proposal_ids": [], "pages_changed": [],
                })

        with mock.patch.object(curation, "_write_journal", side_effect=forge_during_apply):
            with self.assertRaisesRegex(curation.CurationError, "invalid new event"):
                f.publish()
        expected = dict(f.before)
        expected["events/2026/09/evt-forged-name.json"] = forged.read_bytes()
        self.assertEqual(expected, f.vault_bytes())

    def test_foreign_intake_completes_while_fence_held_briefly(self):
        """A cooperative contributor waits bounded while another process fences."""
        f = self._fixture()
        holder_code = (
            "import importlib.util, sys, time\n"
            "spec = importlib.util.spec_from_file_location('event_write_lock', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "with module.event_write_fence(sys.argv[2]):\n"
            "    time.sleep(0.8)\n"
        )
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        holder = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-c", holder_code,
             str(Path(curation.event_write_lock.__file__)), str(f.vault)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        try:
            time.sleep(0.2)  # let the holder acquire the fence first
            start = time.monotonic()
            path = curation.knowledge.write_event(
                f.vault, "other-agent", "stage-source", "Bounded wait intake")
            waited = time.monotonic() - start
            holder.communicate(timeout=60)
        finally:
            if holder.poll() is None:
                holder.kill()
        self.assertEqual(holder.returncode, 0)
        self.assertTrue(path.is_file())
        self.assertGreater(waited, 0.3)
        self.assertLess(waited, 10.0)


if __name__ == "__main__":
    unittest.main()
