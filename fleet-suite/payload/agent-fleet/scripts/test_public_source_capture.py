from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet
import public_source_capture


COMMIT = "a" * 40
PUBLIC_PATH = "inputs/upstream/src/secrets/store/secret-store.ts"
SOURCE_BYTES = b"export const marker = 1;\n"
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()
PUBLIC_URL = (
    "https://raw.githubusercontent.com/example/public-source/"
    + COMMIT
    + "/inputs/upstream/src/secrets/store/secret-store.ts"
)


def sample_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Reviewed the bounded quality subject.",
        "files_changed": [],
        "verification": ["Read the frozen quality evidence."],
        "risks": [],
        "next_steps": [],
    }


class FakeTransport:
    def __init__(
        self,
        mapping: dict[str, bytes] | None = None,
        *,
        redirects: set[str] | None = None,
    ) -> None:
        self.mapping = mapping or {}
        self.redirects = redirects or set()
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float, max_bytes: int) -> bytes:
        self.calls.append(url)
        if url in self.redirects:
            raise public_source_capture.PublicSourceError(
                "Public source fetch refuses redirects"
            )
        if url not in self.mapping:
            raise public_source_capture.PublicSourceError("Public source blob unavailable")
        data = self.mapping[url]
        if len(data) > max_bytes:
            raise public_source_capture.PublicSourceError(
                "Public source blob exceeds the 2 MiB file limit"
            )
        return data


class PublicSourceCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self._transport = public_source_capture.set_transport(
            FakeTransport({PUBLIC_URL: SOURCE_BYTES})
        )
        self.addCleanup(lambda: public_source_capture.set_transport(self._transport))

    def _git_worktree(self, root: Path, relative: str, payload: bytes) -> Path:
        worktree = root / "evidence" / "worker-evidence" / "worktree"
        worktree.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fleet-test@example.invalid"],
            cwd=worktree,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fleet Test"],
            cwd=worktree,
            check=True,
        )
        source = worktree / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"export const marker = 0;\n")
        subprocess.run(["git", "add", "--", relative], cwd=worktree, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline"],
            cwd=worktree,
            check=True,
        )
        source.write_bytes(payload)
        return worktree

    def _worker(self, worktree: Path, run_id: str = "run-20260913T010000Z-aaaaaaaaaa") -> dict:
        return {
            "run_id": run_id,
            "root_task_id": "task-worker-0001",
            "agent": "qwen",
            "model": "deepseek-v4-flash-0731",
            "execution_profile_id": "qwen-deepseek-v4-flash-0731",
            "mode": "implement",
            "run_status": "COMPLETED",
            "exit_code": 0,
            "transport_outcome": {"status": "complete"},
            "handoff": sample_handoff(),
            "git_evidence": {"worktree": str(worktree)},
            "token_usage": {"status": "unknown"},
        }

    def _contract(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "group_id": "group-quality-0001",
            "title": "Bounded group",
            "acceptance_criteria": ["Every item passes"],
            "integration_criteria": [],
            "risk": "medium",
            "max_repair_rounds": 2,
        }

    def _manifest(
        self,
        path: Path,
        *,
        worker_run_id: str = "run-20260913T010000Z-aaaaaaaaaa",
        relative: str = PUBLIC_PATH,
        sha256: str = SOURCE_SHA,
        url: str = PUBLIC_URL,
        extra: dict | None = None,
    ) -> Path:
        payload = {
            "schema_version": 1,
            "files": [
                {
                    "worker_run_id": worker_run_id,
                    "path": relative,
                    "sha256": sha256,
                    "url": url,
                    **(extra or {}),
                }
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _prepare(
        self,
        root: Path,
        worktree: Path,
        *,
        manifest: Path | None = None,
        worker: dict | None = None,
    ):
        return fleet.prepare_quality_bundle(
            root,
            gate_id="qgate-20260913T020000Z-bbbbbbbbbb",
            contract=self._contract(),
            worker_records=[worker or self._worker(worktree)],
            profiles=self.profiles,
            previous=None,
            dry_run=False,
            public_source_manifest=manifest,
        )

    def test_default_secret_path_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = self._git_worktree(root, PUBLIC_PATH, SOURCE_BYTES)
            with self.assertRaisesRegex(
                fleet.FleetError,
                "Sensitive changed path cannot enter quality evidence: "
                + PUBLIC_PATH.replace("\\", "/"),
            ):
                self._prepare(root, worktree)

    def test_matching_public_source_is_captured_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = self._git_worktree(root, PUBLIC_PATH, SOURCE_BYTES)
            manifest = self._manifest(root / "public-source.json")
            bundle, result = self._prepare(root, worktree, manifest=manifest)
            snapshot = (
                bundle
                / "snapshots"
                / "task-worker-0001"
                / "inputs"
                / "upstream"
                / "src"
                / "secrets"
                / "store"
                / "secret-store.ts"
            )
            self.assertTrue(snapshot.is_file())
            self.assertEqual(snapshot.read_bytes(), SOURCE_BYTES)
            entry = result["workers"][0]["snapshots"][0]
            self.assertEqual(entry["source_path"], PUBLIC_PATH)
            self.assertEqual(entry["public_source"]["url"], PUBLIC_URL)
            self.assertEqual(entry["public_source"]["commit"], COMMIT)
            self.assertFalse(entry["public_source"]["authorship_identity"])
            self.assertEqual(result["public_source"]["files"][0]["source_path"], PUBLIC_PATH)

    def test_wrong_host_redirect_worker_path_and_hash_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = self._git_worktree(root, PUBLIC_PATH, SOURCE_BYTES)
            bad_host = self._manifest(
                root / "bad-host.json",
                url="https://evil.example/raw/" + COMMIT + "/secret-store.ts",
            )
            with self.assertRaisesRegex(fleet.FleetError, "raw.githubusercontent.com"):
                self._prepare(root, worktree, manifest=bad_host)
            redirect_manifest = self._manifest(root / "redirect.json")
            public_source_capture.set_transport(
                FakeTransport({PUBLIC_URL: SOURCE_BYTES}, redirects={PUBLIC_URL})
            )
            with self.assertRaisesRegex(fleet.FleetError, "refuses redirects"):
                self._prepare(root, worktree, manifest=redirect_manifest)
            public_source_capture.set_transport(
                FakeTransport({PUBLIC_URL: SOURCE_BYTES})
            )
            wrong_worker = self._manifest(
                root / "wrong-worker.json",
                worker_run_id="run-20260913T010000Z-ffffffffff",
            )
            with self.assertRaisesRegex(fleet.FleetError, "worker that is not in this gate"):
                self._prepare(root, worktree, manifest=wrong_worker)
            other = worktree / "module.ts"
            other.write_bytes(b"export const other = 1;\n")
            wrong_path = self._manifest(
                root / "wrong-path.json",
                relative="module.ts",
                sha256=hashlib.sha256(b"export const other = 1;\n").hexdigest(),
                url="https://raw.githubusercontent.com/example/public-source/"
                + COMMIT
                + "/module.ts",
            )
            public_source_capture.set_transport(
                FakeTransport(
                    {
                        "https://raw.githubusercontent.com/example/public-source/"
                        + COMMIT
                        + "/module.ts": b"export const other = 1;\n"
                    }
                )
            )
            with self.assertRaisesRegex(
                fleet.FleetError, "Sensitive changed path cannot enter quality evidence"
            ):
                self._prepare(root, worktree, manifest=wrong_path)
            public_source_capture.set_transport(
                FakeTransport({PUBLIC_URL: SOURCE_BYTES})
            )
            wrong_hash = self._manifest(root / "wrong-hash.json", sha256="b" * 64)
            with self.assertRaisesRegex(fleet.FleetError, "hash mismatch"):
                self._prepare(root, worktree, manifest=wrong_hash)

    def test_duplicate_manifest_and_unmatched_row_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = self._git_worktree(root, PUBLIC_PATH, SOURCE_BYTES)
            payload = {
                "schema_version": 1,
                "files": [
                    {
                        "worker_run_id": "run-20260913T010000Z-aaaaaaaaaa",
                        "path": PUBLIC_PATH,
                        "sha256": SOURCE_SHA,
                        "url": PUBLIC_URL,
                    },
                    {
                        "worker_run_id": "run-20260913T010000Z-aaaaaaaaaa",
                        "path": PUBLIC_PATH,
                        "sha256": SOURCE_SHA,
                        "url": PUBLIC_URL.replace("secret-store.ts", "other.ts"),
                    },
                ],
            }
            duplicate = root / "duplicate.json"
            duplicate.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(fleet.FleetError, "Duplicate public-source"):
                self._prepare(root, worktree, manifest=duplicate)
            extra = {
                "schema_version": 1,
                "files": [
                    {
                        "worker_run_id": "run-20260913T010000Z-aaaaaaaaaa",
                        "path": PUBLIC_PATH,
                        "sha256": SOURCE_SHA,
                        "url": PUBLIC_URL,
                    },
                    {
                        "worker_run_id": "run-20260913T010000Z-aaaaaaaaaa",
                        "path": "docs/readme.md",
                        "sha256": hashlib.sha256(b"hello\n").hexdigest(),
                        "url": "https://raw.githubusercontent.com/example/public-source/"
                        + COMMIT
                        + "/docs/readme.md",
                    },
                ],
            }
            unmatched = root / "unmatched.json"
            unmatched.write_text(json.dumps(extra), encoding="utf-8")
            public_source_capture.set_transport(
                FakeTransport(
                    {
                        PUBLIC_URL: SOURCE_BYTES,
                        "https://raw.githubusercontent.com/example/public-source/"
                        + COMMIT
                        + "/docs/readme.md": b"hello\n",
                    }
                )
            )
            with self.assertRaisesRegex(fleet.FleetError, "did not match a captured"):
                self._prepare(root, worktree, manifest=unmatched)

    def test_mutation_link_ancestor_credential_bytes_and_oversize_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = self._git_worktree(root, PUBLIC_PATH, SOURCE_BYTES)
            manifest = self._manifest(root / "public-source.json")
            original = public_source_capture.read_plain_file

            def mutate(*args, **kwargs):
                data = original(*args, **kwargs)
                source = worktree / PUBLIC_PATH
                source.write_bytes(data + b"changed\n")
                return data

            with mock.patch.object(public_source_capture, "read_plain_file", side_effect=mutate):
                with self.assertRaisesRegex(fleet.FleetError, "changed"):
                    self._prepare(root, worktree, manifest=manifest)
            source = worktree / PUBLIC_PATH
            source.write_bytes(SOURCE_BYTES)

            original_link = fleet.is_filesystem_link

            def linked(path: Path) -> bool:
                return "secrets" in Path(path).parts or original_link(path)

            with mock.patch.object(fleet, "is_filesystem_link", side_effect=linked):
                with self.assertRaisesRegex(fleet.FleetError, "Linked or reparse"):
                    self._prepare(root, worktree, manifest=manifest)

            secret_bytes = b"const token = 'sk-" + b"a" * 32 + b"';\n"
            secret_tree = self._git_worktree(
                root / "secret-root", PUBLIC_PATH, secret_bytes
            )
            secret_url = PUBLIC_URL
            secret_manifest = self._manifest(
                root / "secret-bytes.json",
                sha256=hashlib.sha256(secret_bytes).hexdigest(),
            )
            public_source_capture.set_transport(FakeTransport({secret_url: secret_bytes}))
            with self.assertRaisesRegex(
                fleet.FleetError, "secret material|sensitive"
            ):
                self._prepare(
                    root / "secret-root",
                    secret_tree,
                    manifest=secret_manifest,
                    worker=self._worker(secret_tree),
                )

            huge = b"export const x = 1;\n" + (b"x" * (2 * 1024 * 1024))
            huge_tree = self._git_worktree(root / "huge-root", PUBLIC_PATH, huge)
            huge_manifest = self._manifest(
                root / "huge.json",
                sha256=hashlib.sha256(huge).hexdigest(),
            )
            public_source_capture.set_transport(FakeTransport({PUBLIC_URL: huge}))
            with self.assertRaisesRegex(fleet.FleetError, "2 MiB"):
                self._prepare(
                    root / "huge-root",
                    huge_tree,
                    manifest=huge_manifest,
                    worker=self._worker(huge_tree),
                )

    def test_env_and_key_paths_remain_forbidden_even_with_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_path = "config/.env.local"
            payload = b"export const none = 1;\n"
            manifest = self._manifest(
                root / "env.json",
                relative=env_path,
                sha256=hashlib.sha256(payload).hexdigest(),
                url="https://raw.githubusercontent.com/example/public-source/"
                + COMMIT
                + "/config/.env.local",
            )
            with self.assertRaisesRegex(
                public_source_capture.PublicSourceError, "forbidden credential or key"
            ):
                public_source_capture.load_manifest(fleet, manifest)
            key_path = "certs/service.key"
            key_tree = self._git_worktree(root / "key-root", key_path, payload)
            with self.assertRaisesRegex(
                fleet.FleetError, "Sensitive changed path cannot enter quality evidence"
            ):
                self._prepare(root / "key-root", key_tree)

    def test_cli_threads_optional_public_source_flag(self) -> None:
        parser = fleet.build_parser()
        for action in ("quality-gate", "security-gate"):
            args = parser.parse_args(
                [
                    action,
                    "--contract",
                    "contract.json",
                    "--run-id",
                    "run-20260913T010000Z-aaaaaaaaaa",
                    "--public-source-manifest",
                    "public-source.json",
                ]
            )
            self.assertEqual(args.public_source_manifest, "public-source.json")
            legacy = parser.parse_args(
                [
                    action,
                    "--contract",
                    "contract.json",
                    "--run-id",
                    "run-20260913T010000Z-aaaaaaaaaa",
                ]
            )
            self.assertIsNone(getattr(legacy, "public_source_manifest", None))

    def test_url_validator_refuses_port_query_fragment_and_userinfo(self) -> None:
        with self.assertRaisesRegex(
            public_source_capture.PublicSourceError, "port"
        ):
            public_source_capture.validate_public_url(
                "https://raw.githubusercontent.com:443/example/public/" + COMMIT + "/a.ts"
            )
        with self.assertRaisesRegex(
            public_source_capture.PublicSourceError, "query or fragment"
        ):
            public_source_capture.validate_public_url(PUBLIC_URL + "?token=1")
        with self.assertRaisesRegex(
            public_source_capture.PublicSourceError, "userinfo"
        ):
            public_source_capture.validate_public_url(
                "https://user:pass@raw.githubusercontent.com/example/public/"
                + COMMIT
                + "/a.ts"
            )


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class SlowChunkResponse:
    def __init__(self, chunks: list[bytes], clock: FakeClock, step: float) -> None:
        self.url = PUBLIC_URL
        self.headers: dict[str, str] = {}
        self._chunks = list(chunks)
        self._clock = clock
        self._step = step

    def read(self, size: int = -1) -> bytes:
        del size
        if not self._chunks:
            return b""
        self._clock.value += self._step
        return self._chunks.pop(0)

    def __enter__(self) -> "SlowChunkResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        del args
        return False


class FakeOpener:
    def __init__(self, response: SlowChunkResponse) -> None:
        self._response = response

    def open(self, request: object, timeout: float | None = None) -> SlowChunkResponse:
        del request, timeout
        return self._response


def _reparse_flag() -> int:
    return int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(attributes & _reparse_flag())


def _create_owned_junction(link: Path, target: Path) -> None:
    env = os.environ.copy()
    env["FLEET_TEST_JUNCTION"] = str(link)
    env["FLEET_TEST_JUNCTION_TARGET"] = str(target)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "New-Item -ItemType Junction -Path $env:FLEET_TEST_JUNCTION -Value $env:FLEET_TEST_JUNCTION_TARGET",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "owned junction create failed: "
            + (completed.stderr or completed.stdout or str(completed.returncode))
        )
    if not _is_reparse_point(link):
        raise AssertionError("created junction is missing FILE_ATTRIBUTE_REPARSE_POINT")


def _remove_owned_junction(link: Path) -> None:
    try:
        present = os.path.lexists(os.fspath(link))
    except OSError:
        return
    if not present:
        return
    if not _is_reparse_point(link):
        return
    link.rmdir()


class PublicSourceDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(0.0)
        self.previous_clock = public_source_capture.set_clock(self.clock)
        self.addCleanup(lambda: public_source_capture.set_clock(self.previous_clock))

    def test_fetch_refuses_injected_bytes_after_transport_exceeds_deadline(self) -> None:
        def transport(url: str, *, timeout: float, max_bytes: int) -> bytes:
            del url, timeout, max_bytes
            self.clock.value = 25.0
            return SOURCE_BYTES

        with self.assertRaisesRegex(
            public_source_capture.PublicSourceError,
            "time bound",
        ):
            public_source_capture.fetch_public_blob(
                PUBLIC_URL,
                timeout=20.0,
                max_bytes=public_source_capture.FILE_LIMIT,
                transport=transport,
                deadline=20.0,
                clock=self.clock,
            )

    def test_fetch_boundary_at_timeout_still_accepts_and_over_timeout_refuses(self) -> None:
        def at_bound(url: str, *, timeout: float, max_bytes: int) -> bytes:
            del url, timeout, max_bytes
            self.clock.value = 20.0
            return SOURCE_BYTES

        data = public_source_capture.fetch_public_blob(
            PUBLIC_URL,
            timeout=20.0,
            max_bytes=public_source_capture.FILE_LIMIT,
            transport=at_bound,
            deadline=20.0,
            clock=self.clock,
        )
        self.assertEqual(data, SOURCE_BYTES)

        self.clock.value = 0.0

        def over_bound(url: str, *, timeout: float, max_bytes: int) -> bytes:
            del url, timeout, max_bytes
            self.clock.value = 20.0001
            return SOURCE_BYTES

        with self.assertRaisesRegex(
            public_source_capture.PublicSourceError,
            "time bound",
        ):
            public_source_capture.fetch_public_blob(
                PUBLIC_URL,
                timeout=20.0,
                max_bytes=public_source_capture.FILE_LIMIT,
                transport=over_bound,
                deadline=20.0,
                clock=self.clock,
            )

    def test_default_fetch_slow_chunks_are_bounded_without_real_http(self) -> None:
        slow = SlowChunkResponse(
            [b"export ", b"const marker = 1;\n"],
            self.clock,
            step=12.0,
        )
        with mock.patch.object(
            urllib.request, "build_opener", return_value=FakeOpener(slow)
        ):
            with self.assertRaisesRegex(
                public_source_capture.PublicSourceError,
                "time bound",
            ):
                public_source_capture.default_fetch(
                    PUBLIC_URL,
                    timeout=20.0,
                    max_bytes=public_source_capture.FILE_LIMIT,
                )

        self.clock.value = 0.0
        timely = SlowChunkResponse(
            [b"export ", b"const marker = 1;\n"],
            self.clock,
            step=1.0,
        )
        with mock.patch.object(
            urllib.request, "build_opener", return_value=FakeOpener(timely)
        ):
            data = public_source_capture.default_fetch(
                PUBLIC_URL,
                timeout=20.0,
                max_bytes=public_source_capture.FILE_LIMIT,
            )
        self.assertEqual(data, SOURCE_BYTES)


class PublicSourceJunctionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._transport = public_source_capture.set_transport(
            FakeTransport({PUBLIC_URL: SOURCE_BYTES})
        )
        self.addCleanup(lambda: public_source_capture.set_transport(self._transport))

    def _row(self, relative: str) -> dict[str, str]:
        return {
            "worker_run_id": "run-20260913T010000Z-aaaaaaaaaa",
            "path": relative,
            "sha256": SOURCE_SHA,
            "url": PUBLIC_URL,
            "commit": COMMIT,
        }

    def test_internal_directory_junction_ancestor_is_refused(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows junction regression")
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary) / "worktree"
            real_dir = worktree / "real"
            real_dir.mkdir(parents=True)
            (real_dir / "module.ts").write_bytes(SOURCE_BYTES)
            alias = worktree / "alias"
            try:
                _create_owned_junction(alias, real_dir)
                self.assertTrue(_is_reparse_point(alias))
                self.assertTrue((alias / "module.ts").is_file())
                with self.assertRaisesRegex(
                    public_source_capture.PublicSourceError,
                    "Linked or reparse",
                ):
                    public_source_capture.capture_listed_file(
                        fleet,
                        worktree=worktree,
                        row=self._row("alias/module.ts"),
                        worker_run_id="run-20260913T010000Z-aaaaaaaaaa",
                        relative_text="alias/module.ts",
                        deadline=time.monotonic()
                        + public_source_capture.OPERATION_TIMEOUT,
                    )
            finally:
                _remove_owned_junction(alias)
            self.assertFalse(os.path.lexists(os.fspath(alias)))
            self.assertTrue((real_dir / "module.ts").is_file())

    def test_plain_directory_positive_control_is_captured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary) / "worktree"
            plain = worktree / "plain"
            plain.mkdir(parents=True)
            (plain / "module.ts").write_bytes(SOURCE_BYTES)
            data, provenance = public_source_capture.capture_listed_file(
                fleet,
                worktree=worktree,
                row=self._row("plain/module.ts"),
                worker_run_id="run-20260913T010000Z-aaaaaaaaaa",
                relative_text="plain/module.ts",
                deadline=time.monotonic()
                + public_source_capture.OPERATION_TIMEOUT,
            )
            self.assertEqual(data, SOURCE_BYTES)
            self.assertEqual(provenance["source_path"], "plain/module.ts")
            self.assertFalse(_is_reparse_point(plain))


if __name__ == "__main__":
    unittest.main()
