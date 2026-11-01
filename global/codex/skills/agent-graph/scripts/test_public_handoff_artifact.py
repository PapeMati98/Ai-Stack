import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS))
import public_handoff_artifact as P


class FakeFleet:
    def __init__(self, *, fail_redact=False, wrong_shape=False, raise_once=False):
        self.fail_redact = fail_redact
        self.wrong_shape = wrong_shape
        self.raise_once = raise_once

    def redact_sensitive_value(self, value):
        if self.fail_redact:
            raise RuntimeError("redactor exploded")
        if isinstance(value, str):
            return 42 if self.wrong_shape else value.replace(
                "api_key=secret-value", "api_key=[REDACTED]"
            )
        if isinstance(value, list):
            return value if self.wrong_shape else [
                self.redact_sensitive_value(item) for item in value
            ]
        return value


class PublicHandoffArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp_context = TemporaryDirectory()
        self.addCleanup(self.temp_context.cleanup)
        self.root = Path(self.temp_context.name)
        self.outdir = self.root / "maker"
        self.outdir.mkdir()

    def _evidence(self, *, truncated=("summary",), missing=()):
        return {"status": "partial", "truncated_fields": list(truncated),
                "missing_fields": list(missing)}

    def test_persists_when_summary_was_truncated_by_projection(self):
        long_summary = "x" * 5000  # larger than MAX_HANDOFF_ITEM_BYTES
        raw = {"status": "completed", "summary": long_summary,
               "files_changed": ["src/a.py"], "verification": ["v"],
               "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "persisted")
        path = Path(ref["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.resolve(strict=True),
                         (self.outdir / "public-handoff.json").resolve(strict=True))
        content = path.read_bytes()
        self.assertEqual(len(content), ref["bytes"])
        self.assertEqual(hashlib.sha256(content).hexdigest(), ref["sha256"])
        decoded = json.loads(content.decode("utf-8"))
        self.assertEqual(decoded["summary"], long_summary)
        self.assertEqual(set(decoded.keys()),
                         {"status", "summary", "files_changed",
                          "verification", "risks", "next_steps"})

    def test_polish_multibyte_summary_survives_utf8_roundtrip(self):
        polish_summary = "Łódź" * 4096  # multibyte UTF-8, > MAX_HANDOFF_ITEM_BYTES
        raw = {"status": "completed", "summary": polish_summary,
               "files_changed": [], "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "persisted")
        content = Path(ref["path"]).read_bytes()
        # Polish multibyte (Łódź: 9 UTF-8 bytes per 4 chars) > 16 KiB raw,
        # bounded_handoff would have truncated, persistence keeps all.
        self.assertGreater(len(content), len(polish_summary))
        self.assertEqual(hashlib.sha256(content).hexdigest(), ref["sha256"])
        decoded = json.loads(content.decode("utf-8"))
        self.assertEqual(decoded["summary"], polish_summary)

    def test_emoji_and_surrogate_pair_summary_survives_utf8(self):
        summary = "🦊" * 4096  # 4-byte UTF-8 codepoints
        raw = {"status": "completed", "summary": summary,
               "files_changed": [], "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "persisted")
        decoded = json.loads(Path(ref["path"]).read_bytes().decode("utf-8"))
        self.assertEqual(decoded["summary"], summary)

    def test_secret_in_summary_and_lists_is_redacted_before_persistence(self):
        raw = {"status": "completed",
               "summary": "configure api_key=secret-value here",
               "files_changed": ["api_key=secret-value", "innocent"],
               "verification": [], "risks": ["api_key=secret-value"],
               "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "persisted")
        text = Path(ref["path"]).read_text(encoding="utf-8")
        self.assertNotIn("secret-value", text)
        self.assertIn("[REDACTED]", text)

    def test_ignores_unrecognized_fields_in_raw_handoff(self):
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": [],
               "ignored": "DROP_SENTINEL", "quota": "QUOTA_SENTINEL",
               "trace": "TRACE_SENTINEL"}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "persisted")
        decoded = json.loads(Path(ref["path"]).read_bytes().decode("utf-8"))
        for sentinel in ("DROP_SENTINEL", "QUOTA_SENTINEL", "TRACE_SENTINEL"):
            self.assertNotIn(sentinel, json.dumps(decoded))
        self.assertNotIn("ignored", decoded)
        self.assertNotIn("quota", decoded)

    def test_no_artifact_when_projection_was_clean(self):
        raw = {"status": "completed", "summary": "ok",
               "files_changed": ["a"], "verification": [],
               "risks": [], "next_steps": []}
        evidence = {"status": "available", "truncated_fields": [],
                    "missing_fields": []}
        self.assertIsNone(P.persist_public_handoff(raw, evidence, FakeFleet(), self.outdir))
        self.assertFalse((self.outdir / "public-handoff.json").exists())

    def test_no_artifact_when_evidence_is_not_a_dict(self):
        raw = {"status": "completed", "summary": "ok",
               "files_changed": [], "verification": [], "risks": [], "next_steps": []}
        self.assertIsNone(P.persist_public_handoff(raw, "evidence", FakeFleet(), self.outdir))
        self.assertIsNone(P.persist_public_handoff(raw, None, FakeFleet(), self.outdir))

    def test_refuses_when_required_raw_field_is_missing(self):
        raw = {"status": "completed",
               "files_changed": ["a"], "verification": [], "risks": [], "next_steps": []}
        evidence = self._evidence(truncated=(), missing=["summary"])
        ref = P.persist_public_handoff(raw, evidence, FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "refused-invalid-handoff")
        self.assertFalse((self.outdir / "public-handoff.json").exists())

    def test_refuses_when_outdir_is_missing(self):
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(),
                                       self.root / "no-such-dir")
        self.assertEqual(ref["status"], "refused-outdir")
        self.assertFalse((self.root / "public-handoff.json").exists())

    def test_refuses_when_outdir_is_a_file(self):
        bogus = self.root / "bogus"
        bogus.write_text("not a directory", encoding="utf-8")
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), bogus)
        self.assertEqual(ref["status"], "refused-outdir")

    def test_refuses_when_outdir_is_a_symlink(self):
        target = self.root / "real"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported in this account")
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), link)
        self.assertEqual(ref["status"], "refused-outdir-symlink")

    def test_refuses_when_outdir_parent_is_a_symlink(self):
        real_root = self.root / "real"
        real_root.mkdir()
        real_outdir = real_root / "maker"
        real_outdir.mkdir()
        link_root = self.root / "link-root"
        try:
            link_root.symlink_to(real_root)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported in this account")
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(),
                                       link_root / "maker")
        self.assertEqual(ref["status"], "refused-outdir-symlink")

    def test_refuses_when_outdir_path_is_relative_to_a_symlink_root(self):
        real_root = self.root / "real"
        real_root.mkdir()
        real_outdir = real_root / "maker"
        real_outdir.mkdir()
        link_root = self.root / "link-root"
        try:
            link_root.symlink_to(real_root)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported in this account")
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        # Literal outdir path is plain (no symlink at the leaf), but a parent
        # segment IS a symlink; we still refuse.
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(),
                                       Path(str(link_root / "maker")))
        self.assertEqual(ref["status"], "refused-outdir-symlink")

    def test_refuses_when_target_file_preexists(self):
        preexisting = self.outdir / "public-handoff.json"
        preexisting.write_text("foreign", encoding="utf-8")
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "refused-pre-existing")
        # Foreign content preserved, not overwritten.
        self.assertEqual(preexisting.read_text(encoding="utf-8"), "foreign")

    def test_refuses_oversized_payload(self):
        big = "x" * (P.MAX_PUBLIC_HANDOFF_BYTES + 1)
        raw = {"status": "completed", "summary": big, "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "refused-oversized")
        self.assertGreater(ref["bytes"], P.MAX_PUBLIC_HANDOFF_BYTES)
        self.assertEqual(ref["max_bytes"], P.MAX_PUBLIC_HANDOFF_BYTES)
        self.assertFalse((self.outdir / "public-handoff.json").exists())

    def test_refuses_when_redactor_raises(self):
        raw = {"status": "completed", "summary": "ok", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(),
                                       FakeFleet(fail_redact=True), self.outdir)
        self.assertEqual(ref["status"], "redaction-failed")
        self.assertFalse((self.outdir / "public-handoff.json").exists())

    def test_refuses_when_redactor_is_missing(self):
        class NoRedact:
            pass

        raw = {"status": "completed", "summary": "ok", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), NoRedact(), self.outdir)
        self.assertEqual(ref["status"], "redactor-unavailable")
        self.assertFalse((self.outdir / "public-handoff.json").exists())

    def test_refuses_when_redactor_returns_wrong_type_for_string(self):
        raw = {"status": "completed", "summary": "ok", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(),
                                       FakeFleet(wrong_shape=True), self.outdir)
        self.assertEqual(ref["status"], "redaction-failed")

    def test_refuses_when_redactor_shortens_list(self):
        class ShorteningRedact:
            def redact_sensitive_value(self, value):
                if isinstance(value, list):
                    return []  # length 0 instead of input length
                return value

        raw = {"status": "completed", "summary": "ok",
               "files_changed": ["a", "b"], "verification": [], "risks": [],
               "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(),
                                       ShorteningRedact(), self.outdir)
        self.assertEqual(ref["status"], "redaction-failed")

    def test_refuses_invalid_raw_handoff(self):
        evidence = self._evidence()
        valid = {"status": "completed", "summary": "x", "files_changed": [],
                 "verification": [], "risks": [], "next_steps": []}
        malformed = [{**valid, field: value} for field, value in (
            ("summary", 17), ("files_changed", "not-a-list"),
            ("verification", ["ok", 17]), ("risks", None),
        )]
        for bad in (None, "not a dict", 42, ["also", "bad"], *malformed):
            with self.subTest(bad=bad):
                ref = P.persist_public_handoff(bad, evidence, FakeFleet(), self.outdir)
                self.assertEqual(ref["status"], "refused-invalid-handoff")
        self.assertFalse((self.outdir / "public-handoff.json").exists())

    def test_persisted_artifact_path_is_within_outdir(self):
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "persisted")
        path = Path(ref["path"]).resolve(strict=True)
        self.assertTrue(path.is_relative_to(self.outdir.resolve(strict=True)))
        self.assertEqual(path.name, P.PUBLIC_HANDOFF_FILENAME)
        self.assertEqual(path.parent.name, self.outdir.name)

    def test_persisted_artifact_metadata_lists_six_fields_and_max(self):
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["fields"], list(P.PUBLIC_HANDOFF_FIELDS))
        self.assertEqual(ref["max_bytes"], P.MAX_PUBLIC_HANDOFF_BYTES)
        # Re-hashing the on-disk file yields the same digest recorded inline.
        content = Path(ref["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), ref["sha256"])

    def test_list_field_redaction_preserves_length_and_redacts_each_item(self):
        raw = {"status": "completed", "summary": "ok",
               "files_changed": ["api_key=secret-value", "innocent"],
               "verification": ["api_key=secret-value", "plain"],
               "risks": ["api_key=secret-value"],
               "next_steps": ["api_key=secret-value", "tail"]}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "persisted")
        decoded = json.loads(Path(ref["path"]).read_bytes().decode("utf-8"))
        self.assertEqual(decoded["files_changed"], ["api_key=[REDACTED]", "innocent"])
        self.assertEqual(decoded["verification"], ["api_key=[REDACTED]", "plain"])
        self.assertEqual(decoded["risks"], ["api_key=[REDACTED]"])
        self.assertEqual(decoded["next_steps"], ["api_key=[REDACTED]", "tail"])
        self.assertNotIn("secret-value", Path(ref["path"]).read_text(encoding="utf-8"))

    def test_partial_write_failure_does_not_delete_preexisting_files(self):
        # Create an unrelated sibling file in outdir; if anything goes wrong
        # the helper must never touch it.
        sibling = self.outdir / "sibling.txt"
        sibling.write_text("keep", encoding="utf-8")
        raw = {"status": "completed", "summary": "x", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        with patch.object(P.os, "write", side_effect=OSError("synthetic failure")):
            ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "refused-write")
        self.assertNotIn("sha256", ref)
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep")

    def test_malformed_unicode_refuses_without_artifact(self):
        raw = {"status": "completed", "summary": "\ud800", "files_changed": [],
               "verification": [], "risks": [], "next_steps": []}
        ref = P.persist_public_handoff(raw, self._evidence(), FakeFleet(), self.outdir)
        self.assertEqual(ref["status"], "redaction-failed")
        self.assertFalse((self.outdir / "public-handoff.json").exists())


class TodoFleetWiringTests(unittest.TestCase):
    """The public_artifact reference is only attached by execute()'s maker branch."""

    def setUp(self):
        env = patch.dict(os.environ, {"AGENT_GRAPH_TIER_ROUTING": "0", "AGENT_GRAPH_WATCH_EVENTS": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.temp_context = TemporaryDirectory()
        self.addCleanup(self.temp_context.cleanup)
        self.root = Path(self.temp_context.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _import_todo_fleet(self):
        import importlib.util
        SCRIPTS = Path(__file__).parent
        spec = importlib.util.spec_from_file_location(
            "todo_fleet_under_test", SCRIPTS / "todo_fleet.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_maker_with_clean_projection_does_not_attach_public_artifact(self):
        todo_fleet = self._import_todo_fleet()
        # Mirror FakeFleet from test_todo_fleet.py so execute() can complete.
        from types import SimpleNamespace

        class MiniFleet:
            TOOL_BUDGET_ENFORCING_ADAPTERS = frozenset({"claude"})
            class FleetError(Exception):
                pass
            class RoutingV2Error(Exception):
                pass

            def __init__(self):
                self.DEFAULT_PROFILES = {"maker": {"adapter": "claude"},
                                         "reviewer": {"adapter": "claude"}}
                self.adaptive = SimpleNamespace(normalize_task_profile=lambda v: v)
                self.routing_v2 = SimpleNamespace(
                    resolve_execution_profile=lambda *a, **k: {"model_provider_id": "x/y"},
                    hard_gates=lambda *a, **k: [],
                    RoutingV2Error=self.RoutingV2Error,
                )
                self.quota = {"weekly_remaining_fraction": 0.9,
                              "short_window_remaining_fraction": 0.9,
                              "current": True}
                self.execute_calls = []

            def run_git(self, argv, workspace):
                return SimpleNamespace(
                    returncode=0 if argv[0] != "symbolic-ref" else 1,
                    stdout="abc\n")

            def load_profiles(self, _):
                return self.DEFAULT_PROFILES

            def state_root(self, _):
                return Path("C:/fake-fleet-state")

            def live_readiness(self, profiles, **kwargs):
                return {a: {"ready": True} for a in profiles}, {}

            def current_quota_views(self, root, profiles):
                return {a: self.quota for a in profiles}, {}

            def execute_single(self, *args, **kwargs):
                self.execute_calls.append(kwargs)
                return {"status": "COMPLETED", "model": "m", "run_id": "r",
                        "usage_record": str(self._write_usage()), "handoff": self.handoff}

            def redact_sensitive_value(self, value):
                if isinstance(value, str):
                    return value.replace("api_key=secret-value", "api_key=[REDACTED]")
                if isinstance(value, list):
                    return [self.redact_sensitive_value(i) for i in value]
                return value

            def _write_usage(self):
                path = self.root / "usage.json"
                import todo_graph
                todo_graph.write(path, {
                    "model_identity": {"status": "verified", "expected_model": "m"},
                    "runtime_attestation": {"status": "VERIFIED"},
                    "model_provider_id": "x/y",
                })
                return path

        fleet = MiniFleet()
        fleet.root = self.root
        outdir = self.root / "maker"
        outdir.mkdir()
        request = {
            "goal": "test", "role": "maker",
            "workspace": str(self.workspace),
            "task": {
                "id": "slice-1", "text": "do the thing", "mode": "advisory",
                "scope": ["src"], "done_when": ["evidence"],
                "maker": [{"agent": "maker", "model": "m",
                            "reason": "fit", "score": 80}],
                "reviewer": [{"agent": "reviewer", "model": "m",
                               "reason": "fit", "score": 80}],
                "complexity": "low", "review_complexity": "high",
            },
            "maker": None,
        }
        request["task"]["handoff"] = {"status": "completed", "summary": "ok",
                                       "files_changed": ["src/a.py"],
                                       "verification": ["v"], "risks": [],
                                       "next_steps": []}
        fleet.execute_calls.clear()
        fleet.handoff = request["task"]["handoff"]
        result = todo_fleet.execute(request, outdir, fleet=fleet)
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["handoff_evidence"]["status"], "available")
        self.assertNotIn("public_artifact", result["handoff_evidence"])
        self.assertFalse((outdir / "public-handoff.json").exists())

    def test_maker_with_truncated_summary_persists_redacted_public_artifact(self):
        todo_fleet = self._import_todo_fleet()
        from types import SimpleNamespace

        class MiniFleet:
            TOOL_BUDGET_ENFORCING_ADAPTERS = frozenset({"claude"})
            class FleetError(Exception):
                pass
            class RoutingV2Error(Exception):
                pass

            def __init__(self):
                self.DEFAULT_PROFILES = {"maker": {"adapter": "claude"},
                                         "reviewer": {"adapter": "claude"}}
                self.adaptive = SimpleNamespace(normalize_task_profile=lambda v: v)
                self.routing_v2 = SimpleNamespace(
                    resolve_execution_profile=lambda *a, **k: {"model_provider_id": "x/y"},
                    hard_gates=lambda *a, **k: [],
                    RoutingV2Error=self.RoutingV2Error,
                )
                self.quota = {"weekly_remaining_fraction": 0.9,
                              "short_window_remaining_fraction": 0.9,
                              "current": True}
                self.execute_calls = []

            def run_git(self, argv, workspace):
                return SimpleNamespace(
                    returncode=0 if argv[0] != "symbolic-ref" else 1,
                    stdout="abc\n")

            def load_profiles(self, _):
                return self.DEFAULT_PROFILES

            def state_root(self, _):
                return Path("C:/fake-fleet-state")

            def live_readiness(self, profiles, **kwargs):
                return {a: {"ready": True} for a in profiles}, {}

            def current_quota_views(self, root, profiles):
                return {a: self.quota for a in profiles}, {}

            def execute_single(self, *args, **kwargs):
                self.execute_calls.append(kwargs)
                return {"status": "COMPLETED", "model": "m", "run_id": "r",
                        "usage_record": str(self._write_usage()), "handoff": self.handoff}

            def redact_sensitive_value(self, value):
                if isinstance(value, str):
                    return value.replace("api_key=secret-value", "api_key=[REDACTED]")
                if isinstance(value, list):
                    return [self.redact_sensitive_value(i) for i in value]
                return value

            def _write_usage(self):
                path = self.root / "usage.json"
                import todo_graph
                todo_graph.write(path, {
                    "model_identity": {"status": "verified", "expected_model": "m"},
                    "runtime_attestation": {"status": "VERIFIED"},
                    "model_provider_id": "x/y",
                })
                return path

        fleet = MiniFleet()
        fleet.root = self.root
        outdir = self.root / "maker"
        outdir.mkdir()
        long_summary = "api_key=secret-value " + "x" * 5000
        request = {
            "goal": "test", "role": "maker",
            "workspace": str(self.workspace),
            "task": {
                "id": "slice-1", "text": "do the thing", "mode": "advisory",
                "scope": ["src"], "done_when": ["evidence"],
                "maker": [{"agent": "maker", "model": "m",
                            "reason": "fit", "score": 80}],
                "reviewer": [{"agent": "reviewer", "model": "m",
                               "reason": "fit", "score": 80}],
                "complexity": "low", "review_complexity": "high",
            },
            "maker": None,
        }
        request["task"]["handoff"] = {"status": "completed",
                                       "summary": long_summary,
                                       "files_changed": ["src/a.py"],
                                       "verification": ["v"], "risks": [],
                                       "next_steps": []}
        fleet.handoff = request["task"]["handoff"]
        result = todo_fleet.execute(request, outdir, fleet=fleet)
        self.assertEqual(result["status"], "CANDIDATE")
        evidence = result["handoff_evidence"]
        self.assertIn("summary", evidence["truncated_fields"])
        self.assertIn("public_artifact", evidence)
        ref = evidence["public_artifact"]
        self.assertEqual(ref["status"], "persisted")
        artifact_path = Path(ref["path"])
        self.assertTrue(artifact_path.is_file())
        self.assertTrue(artifact_path.resolve(strict=True).is_relative_to(
            outdir.resolve(strict=True)))
        # Full redacted summary kept on disk; only a compact ref in response.
        self.assertEqual(ref["bytes"], artifact_path.stat().st_size)
        content = artifact_path.read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), ref["sha256"])
        self.assertNotIn("secret-value", content.decode("utf-8"))
        # Bounded inline summary stayed truncated.
        self.assertLessEqual(
            len(result["handoff"]["summary"].encode("utf-8")),
            todo_fleet.MAX_HANDOFF_ITEM_BYTES)


if __name__ == "__main__":
    unittest.main()
