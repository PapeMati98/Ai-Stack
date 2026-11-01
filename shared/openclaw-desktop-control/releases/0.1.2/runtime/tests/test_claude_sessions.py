"""Read-only mirror of Claude Desktop sessions: required behaviours.

Every test runs on a synthetic directory tree built inside a tempdir — no
interaction with ``%USERPROFILE%\\.claude\\projects`` and no network. The
fixtures here mirror the exact shape OpenClaw observed in the field:

* a project-slug directory holding ``<uuid>.jsonl`` files
* ``sessionId`` and the UI ``local_<uuid>`` token inside those files
* occasional corrupted JSON lines and empty files
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.claude_sessions import (  # noqa: E402
    Resolution,
    SessionRecord,
    default_projects_root,
    find_session_by_file_uuid,
    list_sessions,
    resolve_local_id,
)


# --- Fixture helpers ---------------------------------------------------------


def _slug_for(path: str) -> str:
    """Mirror Claude's project slug encoding for a fictitious Windows path."""
    return path.replace("\\", "-").replace(":", "-").replace(" ", "-")


def _write_session(
    projects_root: Path,
    cwd_path: str,
    file_uuid: str,
    records: list,
    *,
    body_extra: str = "",
    raw_bytes: bytes = None,
) -> Path:
    """Create a ``<slug>/<file_uuid>.jsonl`` file with the supplied records.

    ``body_extra`` is appended verbatim to the JSONL so tests can plant a
    ``local_<uuid>`` token in a non-JSON line. ``raw_bytes`` (if given) takes
    precedence over the JSONL rendering and is written verbatim — that is how
    the corruption and empty-file cases are reproduced.
    """
    slug_dir = projects_root / _slug_for(cwd_path)
    slug_dir.mkdir(parents=True, exist_ok=True)
    file_path = slug_dir / f"{file_uuid}.jsonl"
    if raw_bytes is not None:
        file_path.write_bytes(raw_bytes)
        return file_path
    lines = []
    for record in records:
        lines.append(json.dumps(record, ensure_ascii=False))
    payload = "\n".join(lines) + ("\n" if lines else "")
    if body_extra:
        payload = payload + body_extra
    file_path.write_text(payload, encoding="utf-8")
    return file_path


def _user_record(text: str = "hello", session_id: str = "", local_id: str = "") -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
        "localId": local_id,
    }


def _assistant_record(text: str = "hi", session_id: str = "", local_id: str = "") -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "localId": local_id,
    }


def _metadata(cwd: str, session_id: str, local_id: str = "") -> dict:
    return {
        "type": "metadata",
        "sessionId": session_id,
        "cwd": cwd,
        "localId": local_id,
    }


def _directory_fingerprint(root: Path) -> str:
    """Hash every path + bytes under ``root`` so we can detect any write."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        try:
            if path.is_dir():
                h.update(b"D ")
                h.update(str(path.resolve()).encode("utf-8"))
            elif path.is_file():
                h.update(b"F ")
                h.update(str(path.resolve()).encode("utf-8"))
                h.update(path.read_bytes())
            else:
                continue
        except OSError:
            continue
    return h.hexdigest()


# --- Required behaviours -----------------------------------------------------


class ClaudeSessionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ".claude" / "projects"
        self.root.mkdir(parents=True)

    # -- default root discovery ---------------------------------------------

    def test_default_root_uses_userprofile(self):
        with tempfile.TemporaryDirectory() as sandbox:
            fake_home = Path(sandbox)
            previous = os.environ.get("USERPROFILE")
            os.environ["USERPROFILE"] = str(fake_home)
            try:
                root = default_projects_root()
                self.assertEqual(root, fake_home / ".claude" / "projects")
            finally:
                if previous is None:
                    os.environ.pop("USERPROFILE", None)
                else:
                    os.environ["USERPROFILE"] = previous

    def test_missing_root_yields_empty_list_and_unknown_resolution(self):
        with tempfile.TemporaryDirectory() as sandbox:
            missing = Path(sandbox) / "does" / "not" / "exist"
            self.assertEqual(list_sessions(missing), [])
            result = resolve_local_id(
                "local_5f81e3e2-d6c5-469c-bbd0-801757608e18",
                projects_root=missing,
            )
            self.assertEqual(result.status, "unknown")
            self.assertEqual(result.sessions, ())

    # -- listing --------------------------------------------------------------

    def test_listing_reports_cwd_and_last_activity(self):
        first = _write_session(
            self.root,
            "C:\\Users\\Owner\\Projects\\Alpha",
            "11111111-1111-1111-1111-111111111111",
            [
                _metadata("C:\\Users\\Owner\\Projects\\Alpha",
                          "11111111-1111-1111-1111-111111111111"),
                _user_record("hej", "11111111-1111-1111-1111-111111111111"),
            ],
        )
        second = _write_session(
            self.root,
            "C:\\Users\\Owner\\Projects\\Beta",
            "22222222-2222-2222-2222-222222222222",
            [
                _metadata("C:\\Users\\Owner\\Projects\\Beta",
                          "22222222-2222-2222-2222-222222222222"),
            ],
        )
        # Force ``second`` to look older than ``first`` for deterministic sort.
        first_mtime = first.stat().st_mtime
        second_mtime = second.stat().st_mtime
        os.utime(second, (second_mtime - 5, second_mtime - 5))
        os.utime(first, (first_mtime, first_mtime))

        sessions = list_sessions(self.root)
        self.assertEqual(len(sessions), 2)
        by_uuid = {s.file_uuid: s for s in sessions}
        self.assertEqual(
            by_uuid["11111111-1111-1111-1111-111111111111"].working_directory,
            "C:\\Users\\Owner\\Projects\\Alpha",
        )
        self.assertEqual(
            by_uuid["22222222-2222-2222-2222-222222222222"].working_directory,
            "C:\\Users\\Owner\\Projects\\Beta",
        )
        # Freshest first.
        self.assertEqual(
            sessions[0].file_uuid,
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertGreater(sessions[0].last_activity, sessions[1].last_activity)
        for record in sessions:
            self.assertGreater(record.last_activity, 0)
            self.assertEqual(record.projects_root, str(self.root.resolve()))

    def test_empty_session_file_is_listed(self):
        empty = _write_session(
            self.root,
            "C:\\Users\\Owner\\Empty",
            "33333333-3333-3333-3333-333333333333",
            [],
        )
        sessions = list_sessions(self.root)
        self.assertEqual(len(sessions), 1)
        record = sessions[0]
        self.assertEqual(record.file_uuid,
                         "33333333-3333-3333-3333-333333333333")
        self.assertEqual(record.record_count, 0)
        self.assertEqual(record.local_ids, ())
        # Even with zero records the project slug is preserved verbatim.
        self.assertEqual(record.project_slug, _slug_for("C:\\Users\\Owner\\Empty"))
        self.assertEqual(empty.stat().st_size, 0)

    def test_corrupted_json_line_is_skipped_others_kept(self):
        file_path = _write_session(
            self.root,
            "C:\\Users\\Owner\\Corrupt",
            "44444444-4444-4444-4444-444444444444",
            [
                _user_record("first",
                             "44444444-4444-4444-4444-444444444444"),
            ],
        )
        # Append a deliberately broken JSON line plus a follow-up record.
        with file_path.open("a", encoding="utf-8") as fh:
            fh.write("{this is not json}\n")
            fh.write(json.dumps(_assistant_record(
                "second", "44444444-4444-4444-4444-444444444444")) + "\n")
            fh.write("\n")
            fh.write("not even close\n")

        sessions = list_sessions(self.root)
        self.assertEqual(len(sessions), 1)
        record = sessions[0]
        self.assertEqual(record.record_count, 2,
                         "corrupted lines must not abort the scan")

    def test_non_uuid_filenames_are_ignored(self):
        # A file with a non-UUID name should be skipped.
        junk = self.root / _slug_for("C:\\Users\\Owner\\Junk")
        junk.mkdir(parents=True, exist_ok=True)
        (junk / "notes.txt").write_text("not a session", encoding="utf-8")
        (junk / "session.jsonl.bak").write_text("backup", encoding="utf-8")
        # A real session in the same directory must still be found.
        real = _write_session(
            self.root,
            "C:\\Users\\Owner\\Junk",
            "55555555-5555-5555-5555-555555555555",
            [_user_record("hi", "55555555-5555-5555-5555-555555555555")],
        )
        sessions = list_sessions(self.root)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].file_uuid,
                         "55555555-5555-5555-5555-555555555555")

    # -- UI id mapping --------------------------------------------------------

    def test_resolve_local_id_to_correct_session_file(self):
        target_uuid = "995c121a-95b2-4be3-8146-1888d6df3848"
        ui_token = "local_5f81e3e2-d6c5-469c-bbd0-801757608e18"
        target = _write_session(
            self.root,
            "C:\\Users\\Owner\\Bridge",
            target_uuid,
            [
                _metadata(
                    "C:\\Users\\Owner\\Bridge",
                    target_uuid,
                    local_id=ui_token,
                ),
                _user_record(
                    "follow-up",
                    target_uuid,
                    local_id=ui_token,
                ),
            ],
        )
        # A second session must not be confused with the target.
        _write_session(
            self.root,
            "C:\\Users\\Owner\\Other",
            "88888888-8888-8888-8888-888888888888",
            [
                _metadata(
                    "C:\\Users\\Owner\\Other",
                    "88888888-8888-8888-8888-888888888888",
                    local_id="local_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                ),
            ],
        )

        result = resolve_local_id(ui_token, projects_root=self.root)
        self.assertIsInstance(result, Resolution)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(Path(result.sessions[0]).resolve(), target.resolve())
        self.assertEqual(result.reason, "")

        # Round-trip: the file UUID lookup must point at the same file.
        back = find_session_by_file_uuid(target_uuid, projects_root=self.root)
        self.assertIsNotNone(back)
        self.assertEqual(Path(back.file_path).resolve(), target.resolve())

    def test_resolve_local_id_unknown_when_token_absent(self):
        _write_session(
            self.root,
            "C:\\Users\\Owner\\Orphan",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            [
                _user_record(
                    "anything",
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    local_id="local_11111111-2222-3333-4444-555555555555",
                ),
            ],
        )
        result = resolve_local_id(
            "local_99999999-9999-9999-9999-999999999999",
            projects_root=self.root,
        )
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.sessions, ())
        self.assertEqual(result.reason, "token-not-found")

    def test_resolve_local_id_unknown_for_malformed_token(self):
        for bad in (
            "",
            "local_",
            "local_not-a-uuid",
            "5f81e3e2-d6c5-469c-bbd0-801757608e18",  # no prefix
            "remote_5f81e3e2-d6c5-469c-bbd0-801757608e18",
        ):
            with self.subTest(bad=bad):
                result = resolve_local_id(bad, projects_root=self.root)
                self.assertEqual(result.status, "unknown")
                self.assertEqual(result.reason, "malformed-local-id")
                self.assertEqual(result.sessions, ())

    def test_resolve_local_id_ambiguous_when_two_files_share_token(self):
        shared = "local_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        first = _write_session(
            self.root,
            "C:\\Users\\Owner\\ShareA",
            "11111111-aaaa-bbbb-cccc-111111111111",
            [
                _metadata(
                    "C:\\Users\\Owner\\ShareA",
                    "11111111-aaaa-bbbb-cccc-111111111111",
                    local_id=shared,
                ),
            ],
        )
        second = _write_session(
            self.root,
            "C:\\Users\\Owner\\ShareB",
            "22222222-aaaa-bbbb-cccc-222222222222",
            [
                _metadata(
                    "C:\\Users\\Owner\\ShareB",
                    "22222222-aaaa-bbbb-cccc-222222222222",
                    local_id=shared,
                ),
            ],
        )
        result = resolve_local_id(shared, projects_root=self.root)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.reason, "multiple-sessions-reference-token")
        self.assertEqual(
            {Path(p).resolve() for p in result.sessions},
            {first.resolve(), second.resolve()},
        )

    def test_resolve_accepts_uppercase_uuid_and_normalises(self):
        token_upper = "LOCAL_5F81E3E2-D6C5-469C-BBD0-801757608E18"
        file_uuid = "01234567-89ab-cdef-0123-456789abcdef"
        target = _write_session(
            self.root,
            "C:\\Users\\Owner\\Case",
            file_uuid,
            [
                _metadata(
                    "C:\\Users\\Owner\\Case",
                    file_uuid,
                    local_id="local_5f81e3e2-d6c5-469c-bbd0-801757608e18",
                ),
            ],
        )
        result = resolve_local_id(token_upper, projects_root=self.root)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(Path(result.sessions[0]).resolve(), target.resolve())
        self.assertEqual(result.local_id,
                         "local_5f81e3e2-d6c5-469c-bbd0-801757608e18")

    # -- read-only contract ---------------------------------------------------

    def test_module_never_writes_to_projects_directory(self):
        before = _directory_fingerprint(self.root)
        # Seed the directory with a representative corpus.
        target_uuid = "deadbeef-0000-0000-0000-000000000001"
        ui_token = "local_feedface-0000-0000-0000-000000000002"
        seeded = _write_session(
            self.root,
            "C:\\Users\\Owner\\Sealed",
            target_uuid,
            [
                _metadata(
                    "C:\\Users\\Owner\\Sealed",
                    target_uuid,
                    local_id=ui_token,
                ),
                _user_record(
                    "echo",
                    target_uuid,
                    local_id=ui_token,
                ),
                _assistant_record(
                    "echo",
                    target_uuid,
                    local_id=ui_token,
                ),
            ],
        )
        # Plant a corrupted line and a token-only line.
        with seeded.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write("mention of local_feedface-0000-0000-0000-000000000002\n")
        before_with_seed = _directory_fingerprint(self.root)

        # Exercise every public entry point.
        sessions = list_sessions(self.root)
        self.assertTrue(sessions)
        result = resolve_local_id(ui_token, projects_root=self.root)
        self.assertEqual(result.status, "resolved")
        back = find_session_by_file_uuid(target_uuid, projects_root=self.root)
        self.assertIsNotNone(back)
        # And again with a token that does not exist.
        miss = resolve_local_id(
            "local_00000000-0000-0000-0000-000000000000",
            projects_root=self.root,
        )
        self.assertEqual(miss.status, "unknown")

        after = _directory_fingerprint(self.root)
        self.assertEqual(
            before_with_seed,
            after,
            "claude_sessions must not write to the projects directory",
        )
        # And the root from before any seeding must still differ from after —
        # i.e. nothing outside the seed write touched the tree.
        self.assertNotEqual(before, after)

    # -- scan-window resilience ----------------------------------------------

    def test_token_past_scan_window_is_not_found_but_file_still_listed(self):
        body_lines = []
        for i in range(2000):
            body_lines.append(json.dumps(
                {"type": "user", "i": i, "pad": "x" * 32}))
        deep_token = "local_dec0ded0-1111-2222-3333-444444444444"
        body_lines.append(json.dumps({
            "type": "user",
            "sessionId": "dec0ded0-1111-2222-3333-444444444444",
            "localId": deep_token,
        }))
        raw = ("\n".join(body_lines) + "\n").encode("utf-8")
        self.assertGreater(len(raw), 64 * 1024,
                           "fixture must exceed SCAN_BYTES to be meaningful")
        file_path = _write_session(
            self.root,
            "C:\\Users\\Owner\\Deep",
            "dec0ded0-1111-2222-3333-444444444444",
            [],
            raw_bytes=raw,
        )
        sessions = list_sessions(self.root)
        self.assertEqual(len(sessions), 1)
        record = sessions[0]
        # iter_records walks the entire file, so the body record count is
        # accurate even though the UI token itself sits past SCAN_BYTES.
        self.assertEqual(record.record_count, 2001)
        # Local id past the scan window cannot be matched — that is the
        # documented limit. The file is still listed.
        self.assertEqual(record.local_ids, ())
        miss = resolve_local_id(deep_token, projects_root=self.root)
        self.assertEqual(miss.status, "unknown")
        # The file itself is still resolvable by its file UUID.
        same = find_session_by_file_uuid(
            "dec0ded0-1111-2222-3333-444444444444",
            projects_root=self.root,
        )
        self.assertIsNotNone(same)
        self.assertEqual(Path(same.file_path).resolve(), file_path.resolve())


if __name__ == "__main__":
    unittest.main()
