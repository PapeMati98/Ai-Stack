"""Contract tests for AgentFleetArgvTransport.

Runners are injected: these tests never spawn a real Fleet process.
Identity is taken only from the parsed Fleet JSON envelope.
"""

import json
import logging
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.config import TransportSpec
from caretaker.transports import (
    AgentFleetArgvTransport,
    TransportError,
)


def _spec(argv=None, executable="agent-fleet"):
    return TransportSpec(
        kind="openclaw_argv",
        executable=executable,
        argv=argv or ["agent", "exec", "--json"],
    )


def _ok_envelope(**over):
    base = {
        "ok": True,
        "status": "ok",
        "final": '{"decision":"accept"}',
        "model": "grok-4.6",
        "provider": "grok",
    }
    base.update(over)
    return json.dumps(base)


class AgentFleetArgvTransportTests(unittest.TestCase):

    def test_envelope_provider_and_model_come_from_fleet_response(self):
        declared_provider, declared_model = "muse-code", "muse-spark-1.3"
        assistant_claim = (
            "I am muse-code/muse-spark-1.3 according to this text"
        )

        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _ok_envelope(
                provider="grok",
                model="grok-4.6",
                final=json.dumps({"note": assistant_claim}),
            ), ""

        transport = AgentFleetArgvTransport(
            run_callable=runner,
            expected_identity=(declared_provider, declared_model),
        )
        # Constructor declaration must not override Fleet identity: mismatch.
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_spec(), cwd="c", prompt="secret-prompt-body",
                run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "identity_mismatch")

        matching = AgentFleetArgvTransport(
            run_callable=runner,
            expected_identity=("grok", "grok-4.6"),
        )
        env = matching.invoke(
            spec=_spec(), cwd="c", prompt="secret-prompt-body",
            run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(env.provider, "grok")
        self.assertEqual(env.model, "grok-4.6")
        self.assertNotEqual(env.provider, declared_provider)
        self.assertNotEqual(env.model, declared_model)
        self.assertIn(assistant_claim, env.final)

    def test_identity_mismatch_uses_stable_reason_code(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _ok_envelope(
                provider="minimax", model="MiniMax-M3"), ""

        transport = AgentFleetArgvTransport(
            run_callable=runner,
            expected_identity=("grok", "grok-4.6"),
        )
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_spec(), cwd="c", prompt="hello",
                run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "identity_mismatch")

    def test_process_timeout_and_nonzero_exit_are_distinct(self):
        def timeout_runner(*, executable, argv, cwd, prompt, timeout):
            raise subprocess.TimeoutExpired(cmd=executable, timeout=timeout)

        transport = AgentFleetArgvTransport(run_callable=timeout_runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_spec(), cwd="c", prompt="hello",
                run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "process_timeout")

        def exit_runner(*, executable, argv, cwd, prompt, timeout):
            return 7, "{}", "boom"

        transport = AgentFleetArgvTransport(run_callable=exit_runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_spec(), cwd="c", prompt="hello",
                run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "nonzero_exit")
        self.assertNotEqual("process_timeout", cm.exception.reason_code)

    def test_prompt_body_is_not_logged(self):
        secret = "UNIQUE-PROMPT-BODY-DO-NOT-LOG-9f3a"

        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _ok_envelope(), ""

        transport = AgentFleetArgvTransport(
            run_callable=runner,
            expected_identity=("grok", "grok-4.6"),
        )
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
                records.append(self.format(record))

        handler = _Capture()
        logger = logging.getLogger("caretaker.transports")
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            transport.invoke(
                spec=_spec(), cwd="c", prompt=secret,
                run_config_path="r", state_dir="s", timeout=120)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        joined = "\n".join(records)
        self.assertNotIn(secret, joined)
        self.assertTrue(any("prompt_bytes" in msg for msg in records))


if __name__ == "__main__":
    unittest.main()
