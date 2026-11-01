"""Regression tests for the guarded Kimi run config loop_control section.

Two defects shipped in the guarded ``KIMI_CODE_HOME`` config written by
``fleet.build_guarded_kimi_config``:

1. The ``max_retries_per_step`` key is deprecated and ignored by Kimi CLI
   0.43.1 ("rename it to 'max_attempts_per_step'"). The CLI retries while
   ``attempt < max_attempts_per_step`` with attempts starting at 1, so the
   intended 4 retries require ``max_attempts_per_step = 5``.
2. A flat ``max_steps_per_turn = 64`` killed an implement run on 2026-09-24
   after 27.7 minutes of real work ("loop.max_steps_exceeded: Turn exceeded
   maxSteps=64"). The step budget must depend on the mode: implement runs
   edit and verify in a loop and need far more steps than the read-only
   advisory/review runs. The task timeout stays the real safeguard.

Each test fails on the pre-fix implementation (the config still carries
``max_retries_per_step`` and the flat cap of 64) and passes after the fix.
No real Kimi CLI is launched; the config text is built from a synthetic
source config with a managed OAuth provider.
"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


SOURCE_CONFIG = """
default_model = "k3"

[providers.moonshot]
type = "kimi"
base_url = "https://api.example.test/v1"

[providers.moonshot.oauth]
type = "device"

[models.k3]
provider = "moonshot"
model = "k3-test"
"""


class KimiLoopControlTests(unittest.TestCase):
    """The guarded run config must use live loop_control keys with mode-scaled budgets."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_config = self.root / "config.toml"
        self.source_config.write_text(SOURCE_CONFIG, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _loop_control(self, mode: str) -> dict:
        text = fleet.build_guarded_kimi_config(
            self.source_config,
            model="k3",
            mode=mode,
            native_subagent_limit=0,
            enable_web=False,
        )
        config = tomllib.loads(text)
        return config["loop_control"]

    def test_implement_config_uses_live_retry_key(self) -> None:
        loop = self._loop_control("implement")
        self.assertNotIn(
            "max_retries_per_step",
            loop,
            "Kimi CLI 0.43.1 ignores the deprecated max_retries_per_step key",
        )
        # Attempts start at 1 and retry while attempt < max_attempts_per_step,
        # so 5 attempts deliver the intended 4 retries.
        self.assertEqual(loop["max_attempts_per_step"], 5)

    def test_advisory_config_uses_live_retry_key(self) -> None:
        loop = self._loop_control("advisory")
        self.assertNotIn("max_retries_per_step", loop)
        self.assertEqual(loop["max_attempts_per_step"], 5)

    def test_step_budget_depends_on_mode(self) -> None:
        implement = self._loop_control("implement")["max_steps_per_turn"]
        advisory = self._loop_control("advisory")["max_steps_per_turn"]
        review = self._loop_control("review")["max_steps_per_turn"]
        # The flat cap of 64 killed a real implement run after 27.7 min on
        # 2026-09-24; implement must get strictly more than read-only modes.
        self.assertGreater(implement, advisory)
        self.assertGreater(implement, review)
        self.assertGreater(
            implement,
            64,
            "Implement budget must exceed the cap that aborted the 2026-09-24 run",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
