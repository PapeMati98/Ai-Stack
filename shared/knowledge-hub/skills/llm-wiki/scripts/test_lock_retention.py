"""Release the exact owned lock while preserving its bytes and foreign locks."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("knowledge", Path(__file__).with_name("knowledge.py"))
knowledge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(knowledge)


class LockRetentionTests(unittest.TestCase):
    def test_opsync_busy_preserves_active_lock_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "checkpoint.lock"
            data = b'{"lock_id":"owned"}\n'
            p.write_bytes(data)
            snapshot = knowledge.lock_snapshot(p)
            with patch.object(
                knowledge,
                "checkpoint_lock_opsync",
                side_effect=knowledge.GatewayError("CHECKPOINT_LOCK_OPS_BUSY"),
            ):
                self.assertFalse(knowledge.remove_owned_checkpoint_lock(p, "owned", snapshot[1]))
                self.assertIsNone(
                    knowledge.acquire_checkpoint_lock(p, {"machine": "test"}, recover_stale_lock=False)
                )
            self.assertEqual(p.read_bytes(), data)
            self.assertEqual(list(Path(d).glob("checkpoint.lock.release-*")), [])

    def test_release_preserves_bytes_and_allows_next_owner(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "checkpoint.lock"
            data = b'{"lock_id":"owned","pid":1}\n'
            p.write_bytes(data)
            snapshot = knowledge.lock_snapshot(p)
            with patch.object(Path, "unlink", side_effect=AssertionError("No deletion in release")):
                self.assertTrue(knowledge.remove_owned_checkpoint_lock(p, "owned", snapshot[1]))
            self.assertFalse(p.exists())
            kept = list(Path(d).glob("checkpoint.lock.release-*"))
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].read_bytes(), data)
            p.write_text('{"lock_id":"next"}')
            self.assertFalse(knowledge.remove_owned_checkpoint_lock(p, "owned", snapshot[1]))
            self.assertEqual(json.loads(p.read_text())["lock_id"], "next")
            self.assertEqual(kept[0].read_bytes(), data)

    def test_replaced_lock_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "checkpoint.lock"
            p.write_text('{"lock_id":"owned"}')
            snapshot = knowledge.lock_snapshot(p)
            replacement = Path(d) / "new.lock"
            replacement.write_text('{"lock_id":"foreign"}')
            replacement.replace(p)
            self.assertFalse(knowledge.remove_owned_checkpoint_lock(p, "owned", snapshot[1]))
            self.assertEqual(json.loads(p.read_text())["lock_id"], "foreign")
            self.assertEqual(list(Path(d).glob("checkpoint.lock.release-*")), [])

    def test_pre_move_foreign_replacement_cannot_quarantine_foreign_or_activate_third(self):
        """Deterministic release race: foreign swaps in before the name move.

        Baseline interleaving (check, foreign replace, move, third O_EXCL) used
        to yield release=false, active=third, quarantined=foreign.
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "checkpoint.lock"
            owned_bytes = b'{"lock_id":"owned","pid":1}\n'
            p.write_bytes(owned_bytes)
            snapshot = knowledge.lock_snapshot(p)
            foreign = Path(d) / "foreign.lock"
            foreign_bytes = b'{"lock_id":"foreign","pid":2}\n'
            foreign.write_bytes(foreign_bytes)
            third = {"attempted": False, "acquired": False}

            real_open = knowledge._open_owned_checkpoint_lock

            def interleaved_open(lock_path, lock_id, expected_identity):
                # Another process replaces the name before this release opens
                # and verifies the owned file object. The old name-based code
                # could then quarantine the foreign lock after its first check.
                foreign.replace(lock_path)
                return real_open(lock_path, lock_id, expected_identity)

            with patch.object(knowledge, "_open_owned_checkpoint_lock", side_effect=interleaved_open):
                released = knowledge.remove_owned_checkpoint_lock(p, "owned", snapshot[1])

            # A third writer tries to claim the path after the release attempt.
            third["attempted"] = True
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                third["acquired"] = False
            else:
                os.close(fd)
                third["acquired"] = True

            self.assertTrue(third["attempted"])
            self.assertEqual(json.loads(p.read_text())["lock_id"], "foreign")
            self.assertEqual(p.read_bytes(), foreign_bytes)
            self.assertFalse(third["acquired"], "third lock must not become active after a pre-move foreign swap")
            self.assertEqual(list(Path(d).glob("checkpoint.lock.release-*")), [])
            self.assertFalse(released)

    def test_foreign_swap_after_verified_open_preserves_active_name(self):
        """The Windows handle rename must never move a later foreign name."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "checkpoint.lock"
            p.write_bytes(b'{"lock_id":"owned"}\n')
            snapshot = knowledge.lock_snapshot(p)
            foreign = Path(d) / "foreign.lock"
            foreign_bytes = b'{"lock_id":"foreign"}\n'
            foreign.write_bytes(foreign_bytes)
            real_rename = knowledge._rename_owned_checkpoint_lock

            def swapped_rename(descriptor, lock_path, quarantine):
                # Windows cannot atomically replace the open destination. First
                # move the open owned object away by handle, as an external
                # actor with a DELETE-capable handle could, then claim the name.
                displaced = Path(d) / "owned-displaced.lock"
                real_rename(descriptor, lock_path, displaced)
                foreign.replace(lock_path)
                return real_rename(descriptor, lock_path, quarantine)

            with patch.object(knowledge, "_rename_owned_checkpoint_lock", side_effect=swapped_rename):
                knowledge.remove_owned_checkpoint_lock(p, "owned", snapshot[1])

            self.assertEqual(p.read_bytes(), foreign_bytes)
            for retained in Path(d).glob("checkpoint.lock.release-*"):
                self.assertNotEqual(retained.read_bytes(), foreign_bytes)
            with self.assertRaises(FileExistsError):
                os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    def test_normal_owned_release_then_third_acquires(self):
        """Deterministic happy-path interleaving: clean release, then third create."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "checkpoint.lock"
            owned_bytes = b'{"lock_id":"owned","pid":1}\n'
            p.write_bytes(owned_bytes)
            snapshot = knowledge.lock_snapshot(p)
            third = {"acquired": False, "lock_id": None}

            real_rename = knowledge._rename_owned_checkpoint_lock

            def interleaved_rename(descriptor, lock_path, quarantine):
                real_rename(descriptor, lock_path, quarantine)
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    third["acquired"] = False
                else:
                    os.write(fd, b'{"lock_id":"third"}\n')
                    os.close(fd)
                    third["acquired"] = True
                    third["lock_id"] = json.loads(lock_path.read_text())["lock_id"]

            with patch.object(knowledge, "_rename_owned_checkpoint_lock", side_effect=interleaved_rename):
                released = knowledge.remove_owned_checkpoint_lock(p, "owned", snapshot[1])

            self.assertTrue(released)
            self.assertTrue(third["acquired"])
            self.assertEqual(third["lock_id"], "third")
            self.assertEqual(json.loads(p.read_text())["lock_id"], "third")
            kept = list(Path(d).glob("checkpoint.lock.release-*"))
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].read_bytes(), owned_bytes)


if __name__ == "__main__":
    unittest.main()
