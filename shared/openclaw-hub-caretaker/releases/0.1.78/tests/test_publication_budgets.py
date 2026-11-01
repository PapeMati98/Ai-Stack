"""Publication budget defaults from the 2026-09-20 local-model measurement."""

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.config import (
    ConfigError,
    DEFAULT_PUBLICATION_MAKER_TIMEOUT_SECONDS,
    DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS,
    DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
    OUTER_PUBLICATION_BUDGET_SECONDS,
    load_config,
)


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXAMPLE = ROOT / "config.example.json"


def _write_cfg(raw: dict, directory: str) -> str:
    path = Path(directory) / "c.json"
    path.write_text(json.dumps(raw), "utf-8")
    return str(path)


class PublicationBudgetTests(unittest.TestCase):

    def test_publication_section_without_timeout_fields_uses_measured_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = directory
            raw["publication"] = {}
            cfg = load_config(_write_cfg(raw, directory))
            self.assertEqual(cfg.publication.maker_timeout_seconds, 900)
            self.assertEqual(cfg.publication.reviewer_timeout_seconds, 600)
            self.assertEqual(
                cfg.publication.maker_timeout_seconds,
                DEFAULT_PUBLICATION_MAKER_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                cfg.publication.reviewer_timeout_seconds,
                DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS,
            )

    def test_measured_default_sum_is_accepted_by_upper_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = directory
            raw["publication"] = {
                "maker_timeout_seconds": 900,
                "reviewer_timeout_seconds": 600,
            }
            cfg = load_config(_write_cfg(raw, directory))
            self.assertEqual(cfg.publication.maker_timeout_seconds, 900)
            self.assertEqual(cfg.publication.reviewer_timeout_seconds, 600)
            self.assertLessEqual(
                900 + 600,
                OUTER_PUBLICATION_BUDGET_SECONDS,
            )
            self.assertEqual(OUTER_PUBLICATION_BUDGET_SECONDS, 1800)

    def test_config_without_new_fields_keeps_legacy_360(self):
        raw = json.loads(EXAMPLE.read_text("utf-8"))
        self.assertNotIn("publication", raw)
        self.assertNotIn("maker_timeout_seconds", raw)
        self.assertNotIn("reviewer_timeout_seconds", raw)
        cfg = load_config(str(EXAMPLE))
        self.assertEqual(cfg.publication.maker_timeout_seconds, 360)
        self.assertEqual(cfg.publication.reviewer_timeout_seconds, 360)
        self.assertEqual(
            cfg.publication.maker_timeout_seconds,
            DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            cfg.publication.reviewer_timeout_seconds,
            DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
        )

    def test_sum_exceeding_upper_guard_is_rejected_with_clear_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = directory
            raw["publication"] = {
                "maker_timeout_seconds": 900,
                "reviewer_timeout_seconds": 901,
            }
            with self.assertRaises(ConfigError) as ctx:
                load_config(_write_cfg(raw, directory))
            message = str(ctx.exception)
            self.assertIn("must be <", message)
            self.assertIn(str(OUTER_PUBLICATION_BUDGET_SECONDS), message)
            self.assertIn("maker_timeout_seconds", message)
            self.assertIn("reviewer_timeout_seconds", message)


if __name__ == "__main__":
    unittest.main()
