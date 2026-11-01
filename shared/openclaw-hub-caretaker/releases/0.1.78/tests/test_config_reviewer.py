"""Publication reviewer and timeout config tests."""

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.config import (
    ConfigError,
    DEFAULT_PUBLICATION_FLOOR_TIMEOUT_SECONDS,
    DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS,
    DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
    load_config,
)


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXAMPLE = ROOT / "config.example.json"


def _write_cfg(raw: dict, directory: str) -> str:
    p = Path(directory) / "c.json"
    p.write_text(json.dumps(raw), "utf-8")
    return str(p)


class PublicationReviewerConfigTests(unittest.TestCase):

    def test_missing_publication_keeps_muse_defaults(self):
        raw = json.loads(EXAMPLE.read_text("utf-8"))
        self.assertNotIn("publication", raw)
        cfg = load_config(str(EXAMPLE))
        self.assertEqual(cfg.publication.reviewer.mode, "muse")
        self.assertEqual(cfg.publication.reviewer.candidates, [])
        self.assertEqual(
            cfg.publication.maker_timeout_seconds,
            DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            cfg.publication.reviewer_timeout_seconds,
            DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
        )
        self.assertEqual(cfg.fallback.allowed_backends, ["muse-code"])
        self.assertEqual(cfg.fallback.allowed_models, ["muse-spark-1.3"])

    def test_missing_publication_still_rejects_non_muse_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["fallback"]["allowed_models"] = ["other"]
            raw["state_dir"] = d
            with self.assertRaises(ConfigError):
                load_config(_write_cfg(raw, d))

    def test_fleet_mode_accepts_candidate_list(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["publication"] = {
                "reviewer": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "grok", "model": "grok-4.6"},
                        {"agent": "minimax", "model": "MiniMax-M3"},
                        {"agent": "muse", "model": "muse-spark-1.3"},
                    ],
                }
            }
            cfg = load_config(_write_cfg(raw, d))
            self.assertEqual(cfg.publication.reviewer.mode, "fleet")
            self.assertEqual(len(cfg.publication.reviewer.candidates), 3)
            self.assertEqual(cfg.publication.reviewer.candidates[0].agent, "grok")
            self.assertEqual(cfg.publication.reviewer.candidates[0].model, "grok-4.6")

    def test_fleet_rejects_empty_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["publication"] = {
                "reviewer": {"mode": "fleet", "candidates": []}
            }
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("non-empty", str(ctx.exception))

    def test_fleet_rejects_missing_agent_or_model(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["publication"] = {
                "reviewer": {
                    "mode": "fleet",
                    "candidates": [{"agent": "grok", "model": ""}],
                }
            }
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("model", str(ctx.exception))

            raw["publication"]["reviewer"]["candidates"] = [
                {"agent": "", "model": "grok-4.6"}
            ]
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("agent", str(ctx.exception))

            raw["publication"]["reviewer"]["candidates"] = [
                {"model": "grok-4.6"}
            ]
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("agent", str(ctx.exception))

    def test_fleet_rejects_duplicate_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["publication"] = {
                "reviewer": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "grok", "model": "grok-4.6"},
                        {"agent": "grok", "model": "grok-4.6"},
                    ],
                }
            }
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("duplicate", str(ctx.exception).lower())

    def test_timeouts_positive_accepted_zero_and_negative_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            # 400 + 400 = 800 < outer 1800; the sum-below-outer constraint
            # in ``_validate_publication`` still rejects oversized pairs.
            raw["publication"] = {
                "maker_timeout_seconds": 400,
                "reviewer_timeout_seconds": 400,
            }
            cfg = load_config(_write_cfg(raw, d))
            self.assertEqual(cfg.publication.maker_timeout_seconds, 400)
            self.assertEqual(cfg.publication.reviewer_timeout_seconds, 400)
            self.assertEqual(cfg.publication.reviewer.mode, "muse")

            raw["publication"]["maker_timeout_seconds"] = 0
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("positive", str(ctx.exception))

            raw["publication"]["maker_timeout_seconds"] = 400
            raw["publication"]["reviewer_timeout_seconds"] = -1
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("positive", str(ctx.exception))

    def test_absent_timeout_fields_use_floor_default(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["publication"] = {
                "reviewer": {
                    "mode": "fleet",
                    "candidates": [{"agent": "grok", "model": "grok-4.6"}],
                }
            }
            cfg = load_config(_write_cfg(raw, d))
            # The publication section is present, so the configurable budget
            # default must exceed the legacy 360 s ceiling used when no
            # publication section is supplied.
            self.assertEqual(
                cfg.publication.maker_timeout_seconds,
                DEFAULT_PUBLICATION_FLOOR_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                cfg.publication.reviewer_timeout_seconds,
                DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS,
            )
            self.assertGreater(
                cfg.publication.maker_timeout_seconds,
                DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
            )

    def test_explicit_timeouts_rejected_when_sum_not_below_outer(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["publication"] = {
                "maker_timeout_seconds": 900,
                "reviewer_timeout_seconds": 901,
            }
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, d))
            self.assertIn("must be <", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
