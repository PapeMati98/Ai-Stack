"""Regression tests for Fleet classification and skipped-candidate receipts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caretaker.config import ConfigError
from caretaker.publication import safe_diagnostics
from caretaker.transports import AgentFleetArgvTransport, TransportError
from curate_once import _build_fleet_append_makers, _build_fleet_reviewers

from .test_caretaker_0134 import APPEND, REVIEW, AppendMakerTests, FleetMaker
from .test_curate_fleet_reviewer import _HashPassReviewer, _load_cfg, _mkdir_tmp, _vault


class ClassificationTests(unittest.TestCase):
    def test_config_accepts_public_internal_and_rejects_other_values(self):
        for section in ("append_maker", "reviewer"):
            for value in ("public", "internal"):
                with self.subTest(section=section, value=value), _mkdir_tmp() as d:
                    cfg, _ = _load_cfg(Path(d), {
                        section: {**(APPEND if section == "append_maker" else REVIEW),
                                  "data_classification": value}})
                    self.assertEqual(getattr(cfg.publication, section).data_classification,
                                     value)
            for value in ("confidential", "restricted", "unknown", "", None):
                with self.subTest(section=section, value=value), _mkdir_tmp() as d:
                    with self.assertRaises(ConfigError):
                        _load_cfg(Path(d), {
                            section: {**(APPEND if section == "append_maker" else REVIEW),
                                      "data_classification": value}})

    def test_assessment_uses_selected_runtime_profiles_and_contains_no_task_text(self):
        for role, builder in (("maker", _build_fleet_append_makers),
                              ("reviewer", _build_fleet_reviewers)):
            with self.subTest(role=role), _mkdir_tmp() as d:
                root = Path(d)
                publication = {"append_maker": {**APPEND, "data_classification": "internal"},
                               "reviewer": {**REVIEW, "data_classification": "public"}}
                cfg, _ = _load_cfg(root, publication)
                spec = builder(cfg)[0]["spec"]
                fleet_py = root / "selected" / "agent-fleet" / "scripts" / "fleet.py"
                fleet_py.parent.mkdir(parents=True)
                fleet_py.write_text("# synthetic runtime\n", encoding="utf-8")
                profiles = fleet_py.parent.parent / "references" / "profiles.json"
                profiles.parent.mkdir()
                profiles.write_text(json.dumps({"profiles": {
                    "claude": {}, "muse": {}, "minimax": {}}}), encoding="utf-8")
                captured = {}

                def runner(**kwargs):
                    argv = kwargs["argv"]
                    self.assertIn("--assessment-file", argv)
                    captured["assessment"] = json.loads(Path(
                        argv[argv.index("--assessment-file") + 1]).read_text("utf-8"))
                    captured["argv"] = argv
                    return 0, json.dumps({"status": "DRY_RUN", "adapter": "claude",
                                          "agent": "claude", "model": spec.argv[-1],
                                          "attempt_id": "synthetic"}), ""

                with patch("caretaker.transports.resolve_fleet_py", return_value=fleet_py):
                    AgentFleetArgvTransport(run_callable=runner).invoke(
                        spec=spec, cwd=str(root), prompt="PRIVATE_PAGE_EVIDENCE",
                        run_config_path="", state_dir=str(root), timeout=60)
                assessment = captured["assessment"]
                self.assertEqual(set(assessment["model_scores"]),
                                 {"claude", "muse", "minimax"})
                self.assertEqual(assessment["model_scores"]["claude"]["score"], 90)
                self.assertEqual(assessment["model_scores"]["muse"]["score"], 50)
                self.assertEqual(assessment["data_handling"], {
                    "classification": "internal" if role == "maker" else "public",
                    "anthropic_zdr_authorization": None})
                self.assertEqual(assessment["substantive"], False)
                self.assertEqual(assessment["preflight"]["status"], "not-applicable")
                self.assertEqual(assessment["workload_weights"],
                                 {"docs" if role == "maker" else "review": 100})
                self.assertEqual(assessment["required_capabilities"],
                                 [] if role == "maker" else ["independent-review"])
                self.assertNotIn("PRIVATE_PAGE_EVIDENCE", json.dumps(assessment))

    def test_missing_profiles_fails_without_spawning(self):
        for contents in (None, "broken json", '{"profiles": []}'):
            with self.subTest(contents=contents), _mkdir_tmp() as d:
                root = Path(d)
                cfg, _ = _load_cfg(root, {"append_maker": {
                    **APPEND, "data_classification": "internal"}})
                spec = _build_fleet_append_makers(cfg)[0]["spec"]
                fleet_py = root / "selected" / "agent-fleet" / "scripts" / "fleet.py"
                if contents is not None:
                    profiles = fleet_py.parent.parent / "references" / "profiles.json"
                    profiles.parent.mkdir(parents=True)
                    profiles.write_text(contents, encoding="utf-8")
                calls = []
                with patch("caretaker.transports.resolve_fleet_py", return_value=fleet_py):
                    with self.assertRaises(TransportError) as cm:
                        AgentFleetArgvTransport(run_callable=lambda **kw: calls.append(kw)).invoke(
                            spec=spec, cwd=str(root), prompt="x", run_config_path="",
                            state_dir=str(root), timeout=60)
                self.assertEqual(cm.exception.reason_code, "fleet_profiles_unavailable")
                self.assertEqual(calls, [])

    def test_absent_classification_keeps_bare_argv_without_profiles(self):
        with _mkdir_tmp() as d:
            root = Path(d)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND})
            spec = _build_fleet_append_makers(cfg)[0]["spec"]
            fleet_py = root / "selected" / "agent-fleet" / "scripts" / "fleet.py"
            captured = []

            def runner(**kwargs):
                captured.extend(kwargs["argv"])
                return 0, json.dumps({"status": "DRY_RUN", "adapter": "claude",
                                      "agent": "claude", "model": "sonnet"}), ""

            with patch("caretaker.transports.resolve_fleet_py", return_value=fleet_py):
                AgentFleetArgvTransport(run_callable=runner).invoke(
                    spec=spec, cwd=str(root), prompt="x", run_config_path="",
                    state_dir=str(root), timeout=60)
            self.assertEqual(captured[:2], [str(fleet_py), "run"])
            self.assertEqual(captured[4:], ["--agent", "claude", "--model", "sonnet"])
            self.assertNotIn("--assessment-file", captured)


class AttemptReceiptTests(unittest.TestCase):
    def test_skipped_maker_and_reviewer_are_recorded_without_messages(self):
        class RefusedMaker(FleetMaker):
            def invoke(self, **kwargs):
                self.calls.append(kwargs)
                raise TransportError("PRIVATE_PAGE_EVIDENCE", reason_code="fleet_refused")

        class RefusedReviewer(_HashPassReviewer):
            def invoke(self, **kwargs):
                self.calls.append(kwargs)
                raise TransportError("PRIVATE_PAGE_EVIDENCE", reason_code="process_timeout")

        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            refused = RefusedMaker()
            second = FleetMaker(model="MiniMax-M3", provider="minimax")
            reviewer = RefusedReviewer(provider="claude", model="claude-opus-5-5")
            result = AppendMakerTests._run(self, root, frozen, cfg,
                                           [refused, second],
                                           [reviewer, _HashPassReviewer()])
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["maker_attempts"], [
                {"agent": "claude", "model": "sonnet", "reason_code": "fleet_refused"}])
            self.assertEqual(result["reviewer_attempts"], [
                {"agent": "claude", "model": "claude-opus-5-5",
                 "reason_code": "process_timeout"}])
            self.assertEqual(safe_diagnostics(result)["maker_attempts"],
                             result["maker_attempts"])
            diagnostics = json.loads((root / "run" / "diagnostics.json").read_text("utf-8"))
            self.assertEqual(diagnostics["reviewer_attempts"], result["reviewer_attempts"])
            self.assertNotIn("PRIVATE_PAGE_EVIDENCE", json.dumps(diagnostics))


if __name__ == "__main__":
    unittest.main()
