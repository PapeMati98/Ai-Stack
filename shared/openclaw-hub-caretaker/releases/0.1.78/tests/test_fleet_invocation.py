"""Contract tests for the real Agent Fleet invocation path.

These tests pin the 2026-09-20 fix for ``independent_review_deferred:
spawn_failed``. The previous transport built a non-existent
``agent-fleet.cmd`` argv and inherited the OpenClaw envelope parser, so the
fleet mode always failed before any work happened.

The contract this file proves (mirrored in ``FLEET-INVOCATION.md``):

* The transport spawns ``python <fleet.py> run`` with a prompt staged via
  ``--task-file`` (Fleet reads its task from a file, not from stdin) and
  pins the candidate's ``--agent``/``--model``. No additional flags are
  passed because none of them were observed working in a real Fleet run
  during the 2026-09-20 probe.
* ``AGENT_FLEET_DEPTH`` is stripped from the child env before the spawn;
  Fleet's recursion guard refuses nested attempts otherwise (exit 2).
* The response is mapped onto ``Envelope`` with provider/model/session_id
  taken ONLY from the Fleet JSON (``adapter`` / ``model`` / ``attempt_id``).
* A mismatch against the constructor's ``expected_identity`` raises
  ``TransportError(reason_code="identity_mismatch")``.
* A spawn failure (``OSError``) raises
  ``TransportError(reason_code="spawn_failed")``.
* The prompt body is never emitted to logs.

The test runners are injected so no real ``fleet.py`` is launched.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.config import TransportSpec
from caretaker.transports import (
    AGENT_FLEET_DEPTH_ENV,
    AgentFleetArgvTransport,
    TransportError,
    _fleet_child_env,
    _fleet_default_run,
    resolve_fleet_py,
)


def _fleet_spec(*, agent: str = "grok", model: str = "grok-4.6",
                executable: str = "python.exe") -> TransportSpec:
    """Mirror the production spec built by ``curate_once._build_fleet_reviewers``.

    The 2026-09-20 probe confirmed only ``--task-file`` + ``--agent`` +
    ``--model`` are required for Fleet to return a parseable JSON envelope.
    Other flags documented in Fleet's argparse help (e.g. ``--mode``,
    ``--kind``, ``--complexity``, ``--timeout``, ``--workspace``,
    ``--assessment-file``) were either only exercised in dry-run or never
    exercised at all, so the production spec keeps the bare minimum.
    """
    return TransportSpec(
        kind="openclaw_argv",
        executable=executable,
        argv=[
            "${fleet_py}",
            "run",
            "--task-file", "${task_file}",
            "--agent", agent,
            "--model", model,
        ],
    )


def _fleet_envelope_dry_run(*, agent="grok", adapter="grok",
                            model="grok-4.6",
                            attempt_id="attempt-fleet-7f2c") -> str:
    """Mimic the Fleet ``DRY_RUN`` JSON object the 2026-09-20 probe observed.

    The shape is a strict subset of the documented Fleet response: identity
    is taken from ``adapter``/``model``/``attempt_id`` only, never from the
    prompt echo (``stdin``) or from any text in ``final``.
    """
    payload = {
        "action": "run",
        "adapter": adapter,
        "adapter_preflight": {"adapter": adapter, "status": "NOT_REQUIRED"},
        "agent": agent,
        "attempt_id": attempt_id,
        "command": ["C:/grok.exe", "--cwd", "ws"],
        "mode": "review",
        "model": model,
        "status": "DRY_RUN",
        "stdin": "ECHO-OF-PROMPT-MUST-NOT-LEAK",
        "task_sha256": "deadbeef" * 8,
        "workspace": "ws",
    }
    return json.dumps(payload)


class FleetInvocationArgvTests(unittest.TestCase):
    """The transport builds a correct Fleet invocation."""

    def test_argv_contains_run_subcommand_and_agent_model_flags(self):
        captured: dict = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            captured["executable"] = executable
            captured["argv"] = list(argv)
            captured["cwd"] = cwd
            captured["prompt"] = prompt
            captured["timeout"] = timeout
            return 0, _fleet_envelope_dry_run(), ""

        transport = AgentFleetArgvTransport(run_callable=runner)
        transport.invoke(
            spec=_fleet_spec(),
            cwd="ws",
            prompt="review the candidate",
            run_config_path="rc.json",
            state_dir="state",
            timeout=120,
        )

        argv = captured["argv"]
        # ``argv[0]`` is the resolved ``fleet.py`` path (not
        # ``agent-fleet.cmd``). ``argv[1]`` is the Fleet subcommand.
        self.assertTrue(argv[0].endswith("fleet.py"),
                        f"unexpected fleet entry: {argv[0]!r}")
        self.assertEqual(argv[1], "run")
        # The only validated production flags: --task-file, --agent, --model.
        # We deliberately do NOT pass --mode, --kind, --complexity,
        # --timeout, --workspace, --assessment-file because none of them
        # were observed working in a real Fleet invocation during the
        # 2026-09-20 probe (see FLEET-INVOCATION.md).
        self.assertIn("--task-file", argv)
        self.assertNotEqual(argv[argv.index("--task-file") + 1], "")
        self.assertIn("--agent", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "grok")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "grok-4.6")
        self.assertEqual(argv.count("--timeout"), 1)
        self.assertEqual(argv[argv.index("--timeout") + 1], "110")
        for absent in ("--mode", "--kind", "--complexity",
                       "--workspace", "--assessment-file"):
            self.assertNotIn(absent, argv,
                             f"unvalidated flag leaked into argv: {absent}")
        # spec.executable is the Python interpreter (sys.executable), as
        # produced by ``_build_fleet_reviewers``.
        self.assertEqual(captured["executable"], "python.exe")

    def test_prompt_body_is_staged_via_task_file_and_not_passed_on_stdin(self):
        captured: dict = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            captured["prompt"] = prompt
            captured["argv"] = list(argv)
            # Read the staged task file inside the runner: the transport
            # tears down its TemporaryDirectory once invoke() returns, so
            # we must capture the body here while the file is still live.
            task_idx = captured["argv"].index("--task-file")
            task_path = captured["argv"][task_idx + 1]
            captured["task_file_bytes"] = Path(task_path).read_bytes()
            return 0, _fleet_envelope_dry_run(), ""

        transport = AgentFleetArgvTransport(run_callable=runner)
        secret_body = (
            "REVIEW-PROMPT-DO-NOT-LEAK-via-stdin "
            "9c3e6f2a-UNIQUE-MARKER")
        transport.invoke(
            spec=_fleet_spec(),
            cwd="ws",
            prompt=secret_body,
            run_config_path="rc.json",
            state_dir="state",
            timeout=120,
        )
        # Fleet never reads from stdin; the runner receives an empty prompt.
        self.assertEqual(captured["prompt"], "")
        # The task file was written with the prompt body before the
        # transport tore down its staging directory.
        self.assertEqual(
            captured["task_file_bytes"].decode("utf-8"), secret_body)


class FleetInvocationEnvelopeMappingTests(unittest.TestCase):
    """The Fleet JSON response is mapped to Envelope correctly."""

    def test_identity_taken_from_response_adapter_model_attempt_id(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _fleet_envelope_dry_run(
                adapter="minimax", model="MiniMax-M3",
                attempt_id="attempt-abc-123"), ""

        transport = AgentFleetArgvTransport(
            run_callable=runner,
            expected_identity=("minimax", "MiniMax-M3"),
        )
        envelope = transport.invoke(
            spec=_fleet_spec(),
            cwd="ws",
            prompt="x",
            run_config_path="rc", state_dir="state", timeout=60,
        )
        self.assertEqual(envelope.provider, "minimax")
        self.assertEqual(envelope.model, "MiniMax-M3")
        self.assertEqual(envelope.session_id, "attempt-abc-123")
        self.assertEqual(envelope.status, "ok")
        self.assertTrue(envelope.ok)

    def test_final_uses_parsed_envelope_when_fleet_response_has_no_review(self):
        """The 2026-09-20 probe only observed DRY_RUN preflight responses.
        Those responses have no ``final`` / ``handoff`` field; the
        transport must therefore surface an identity-only slice so the
        candidate loop has the Fleet identity fields at hand. We must NOT
        echo the ``stdin`` field (it's the staged prompt body, not a
        review) and we must NOT read ``--assessment-file`` (it was never
        passed and is unprobed by the probe).
        """
        envelope = AgentFleetArgvTransport(
            run_callable=lambda **kw: (0, _fleet_envelope_dry_run(), ""),
        ).invoke(
            spec=_fleet_spec(),
            cwd="ws",
            prompt="x",
            run_config_path="rc", state_dir="state", timeout=60,
        )
        # The prompt echo MUST NOT leak into ``final``.
        self.assertNotIn("ECHO-OF-PROMPT-MUST-NOT-LEAK", envelope.final)
        # The opaque Fleet-internal ``command`` argv MUST NOT leak either:
        # reading it would couple the transport to a specific adapter.
        self.assertNotIn("grok.exe", envelope.final)
        # The identity fields from the parsed envelope are preserved in
        # ``final`` so the candidate loop can introspect them.
        self.assertIn('"adapter": "grok"', envelope.final)
        self.assertIn('"model": "grok-4.6"', envelope.final)
        self.assertIn('"attempt_id": "attempt-fleet-7f2c"', envelope.final)
        # The DRY_RUN status is preserved too so the candidate loop can see
        # why this particular envelope has no review payload.
        self.assertIn('"status": "DRY_RUN"', envelope.final)

    def test_final_honours_explicit_final_field_when_fleet_returns_one(self):
        """If a future Fleet real-run reply carries an explicit ``final``
        string, the transport MUST honour it verbatim. The probe observed
        no real-run payloads, so this case is a forward-compatibility
        assertion (a literal ``final`` field is preserved as-is)."""

        payload = _fleet_envelope_payload(
            status="OK",
            final='{"verdict":"PASS","manifest_sha256":"'
            + "f" * 64 + '","findings":[]}',
        )

        envelope = AgentFleetArgvTransport(
            run_callable=lambda **kw: (0, json.dumps(payload), ""),
        ).invoke(
            spec=_fleet_spec(),
            cwd="ws",
            prompt="x",
            run_config_path="rc", state_dir="state", timeout=60,
        )
        self.assertIn('"verdict":"PASS"', envelope.final)
        self.assertNotIn("ECHO-OF-PROMPT-MUST-NOT-LEAK", envelope.final)

    def test_identity_mismatch_uses_stable_reason_code(self):
        """A mismatch between Fleet's reported adapter/model and the
        constructor's ``expected_identity`` raises ``identity_mismatch``,
        not a silent pass or a leaked raw body.
        """
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _fleet_envelope_dry_run(
                adapter="grok", model="grok-4.6"), ""

        transport = AgentFleetArgvTransport(
            run_callable=runner,
            expected_identity=("minimax", "MiniMax-M3"),
        )
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_fleet_spec(agent="grok", model="grok-4.6"),
                cwd="ws", prompt="x",
                run_config_path="rc", state_dir="state", timeout=60,
            )
        self.assertEqual(cm.exception.reason_code, "identity_mismatch")
        # No part of the Fleet JSON or the prompt body leaked into the
        # exception message.
        msg = str(cm.exception)
        self.assertNotIn("agent-fleet", msg.lower())
        self.assertNotIn("ECHO-OF-PROMPT-MUST-NOT-LEAK", msg)

    def test_fleet_refusal_maps_to_fleet_refused_reason(self):
        """The 2026-09-20 recursion-guard refusal (``status: REFUSED``) is
        surfaced as ``TransportError(fleet_refused)`` with the Fleet error
        description preserved but no raw envelope bytes leaked.

        0.1.16: a refusal used to share ``nonzero_exit`` with every other
        Fleet failure, so a gate refusal was indistinguishable from a reviewer
        that broke the handoff contract. It is its own, non-repairable code now,
        and still transient in pubstate."""
        refusal = {
            "action": "run",
            "status": "REFUSED",
            "error": "Recursive Agent Fleet invocation is refused",
        }

        def runner(*, executable, argv, cwd, prompt, timeout):
            return 2, "", json.dumps(refusal)

        transport = AgentFleetArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_fleet_spec(),
                cwd="ws", prompt="x",
                run_config_path="rc", state_dir="state", timeout=60,
            )
        self.assertEqual(cm.exception.reason_code, "fleet_refused")
        self.assertIn("Recursive Agent Fleet invocation", str(cm.exception))
        from caretaker.pubstate import classify_failure
        self.assertEqual(classify_failure("fleet_refused"), "transient")


class FleetInvocationSpawnFailureTests(unittest.TestCase):
    """A missing executable or a real spawn error raises ``spawn_failed``."""

    def test_oserror_from_runner_maps_to_spawn_failed(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            raise OSError(2, "No such file or directory", argv[0])

        transport = AgentFleetArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_fleet_spec(),
                cwd="ws", prompt="x",
                run_config_path="rc", state_dir="state", timeout=60,
            )
        self.assertEqual(cm.exception.reason_code, "spawn_failed")
        # The exception message must not carry the prompt body.
        self.assertNotIn("x", str(cm.exception))

    def test_nonzero_exit_with_no_envelope_is_reported(self):
        """A subprocess that exits non-zero without writing a parseable
        JSON envelope is reported as ``nonzero_exit`` (not a silent pass)."""

        def runner(*, executable, argv, cwd, prompt, timeout):
            return 7, "", "boom"

        transport = AgentFleetArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            transport.invoke(
                spec=_fleet_spec(),
                cwd="ws", prompt="x",
                run_config_path="rc", state_dir="state", timeout=60,
            )
        self.assertEqual(cm.exception.reason_code, "nonzero_exit")


class FleetInvocationEnvStrippingTests(unittest.TestCase):
    """The transport MUST clear ``AGENT_FLEET_DEPTH`` in the child env."""

    def test_helper_strips_AGENT_FLEET_DEPTH_from_explicit_base(self):
        base = {"AGENT_FLEET_DEPTH": "1", "PATH": "x", "OTHER": "y"}
        cleaned = _fleet_child_env(base)
        self.assertNotIn(AGENT_FLEET_DEPTH_ENV, cleaned)
        self.assertEqual(cleaned["PATH"], "x")
        self.assertEqual(cleaned["OTHER"], "y")
        # The helper does not mutate the caller's dict.
        self.assertIn(AGENT_FLEET_DEPTH_ENV, base)

    def test_helper_strips_AGENT_FLEET_DEPTH_from_current_env(self):
        saved = os.environ.get(AGENT_FLEET_DEPTH_ENV)
        os.environ[AGENT_FLEET_DEPTH_ENV] = "2"
        try:
            cleaned = _fleet_child_env()
            self.assertNotIn(AGENT_FLEET_DEPTH_ENV, cleaned)
        finally:
            if saved is None:
                os.environ.pop(AGENT_FLEET_DEPTH_ENV, None)
            else:
                os.environ[AGENT_FLEET_DEPTH_ENV] = saved

    def test_default_runner_passes_clean_env_to_subprocess(self):
        """The real ``_fleet_default_run`` strips ``AGENT_FLEET_DEPTH``
        from the env it forwards to ``subprocess.run``.

        We monkey-patch ``subprocess.run`` at the module level to capture
        the env without actually spawning a real Fleet process.
        """
        saved_depth = os.environ.get(AGENT_FLEET_DEPTH_ENV)
        saved_allow = os.environ.get("HUB_CARETAKER_ALLOW_EXEC")
        os.environ[AGENT_FLEET_DEPTH_ENV] = "1"
        os.environ["HUB_CARETAKER_ALLOW_EXEC"] = "1"
        seen: dict = {}
        try:
            real_run = subprocess.run
            from caretaker import transports as t_mod
            original = t_mod.subprocess.run

            def fake_run(args, **kwargs):
                # Capture only the env the transport forwarded. We do NOT
                # propagate the actual Fleet binary because the runner's
                # argv contains the resolved fleet.py path; we just want
                # proof that the env was cleaned.
                seen["env"] = dict(kwargs.get("env") or {})
                return real_run(
                    [sys.executable, "-c", "import sys; sys.exit(0)"],
                    capture_output=True, timeout=5,
                )

            t_mod.subprocess.run = fake_run
            try:
                transport = AgentFleetArgvTransport(
                    run_callable=t_mod._fleet_default_run)
                # The fake subprocess returns an empty stdout/stderr, which
                # the parser reports as ``invalid_envelope``; we only care
                # about the env captured before that failure.
                try:
                    transport.invoke(
                        spec=_fleet_spec(executable=sys.executable),
                        cwd=str(_ROOT),
                        prompt="x",
                        run_config_path="",
                        state_dir=str(_ROOT / ".test-tmp"),
                        timeout=120,
                    )
                except TransportError:
                    pass
            finally:
                t_mod.subprocess.run = original
        finally:
            if saved_depth is None:
                os.environ.pop(AGENT_FLEET_DEPTH_ENV, None)
            else:
                os.environ[AGENT_FLEET_DEPTH_ENV] = saved_depth
            if saved_allow is None:
                os.environ.pop("HUB_CARETAKER_ALLOW_EXEC", None)
            else:
                os.environ["HUB_CARETAKER_ALLOW_EXEC"] = saved_allow
        self.assertTrue(seen, "subprocess.run was not invoked")
        self.assertNotIn(AGENT_FLEET_DEPTH_ENV, seen["env"],
                         "AGENT_FLEET_DEPTH leaked into child env")


class FleetInvocationLogSanitizationTests(unittest.TestCase):
    """The prompt body never reaches the logs."""

    def test_prompt_body_not_in_logs(self):
        secret = "UNIQUE-PROMPT-BODY-DO-NOT-LOG-IN-FLEET-2e9f"

        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _fleet_envelope_dry_run(), ""

        transport = AgentFleetArgvTransport(run_callable=runner)
        records: list = []

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
                spec=_fleet_spec(),
                cwd="ws", prompt=secret,
                run_config_path="rc", state_dir="state", timeout=60,
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        joined = "\n".join(records)
        self.assertNotIn(secret, joined)
        # Metadata about the call IS in the logs.
        self.assertTrue(any("prompt_bytes" in msg for msg in records),
                        "no prompt_bytes metadata in logs")


class FleetResolutionTests(unittest.TestCase):
    """The fleet.py resolver honours ``AGENT_FLEET_SUITE_ROOT``."""

    def test_default_resolution_uses_attested_runtime(self):
        from caretaker.transports import RUNTIME_MARKER, runtime_candidate
        parent = _ROOT / '.test-tmp'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(parent)) as directory:
            root = Path(directory) / 'lean-test'
            entry = root / 'agent-fleet/scripts/fleet.py'
            entry.parent.mkdir(parents=True)
            entry.write_text('# synthetic fleet\n', encoding='utf-8')
            (root / RUNTIME_MARKER).write_text(json.dumps({
                'package_id': 'agent-fleet-suite',
                'managed_by': 'agent-fleet-suite-v2-installer',
                'package_fingerprint': 'synthetic', 'payload_fingerprint': 'synthetic',
                'transaction_id': 'synthetic', 'package_version': '0.9.99',
                'payload_file_count': 1,
            }), encoding='utf-8')
            with patch.dict(os.environ, {'AGENT_FLEET_SUITE_PIN': ''}):
                saved = os.environ.pop('AGENT_FLEET_SUITE_ROOT', None)
                try:
                    with patch('caretaker.transports.FLEET_RUNTIME_PARENT', Path(directory)):
                        self.assertEqual(resolve_fleet_py(), entry)
                        self.assertIsNotNone(runtime_candidate(root))
                finally:
                    if saved is None:
                        os.environ.pop('AGENT_FLEET_SUITE_ROOT', None)
                    else:
                        os.environ['AGENT_FLEET_SUITE_ROOT'] = saved

    def test_unattested_file_is_not_accepted(self):
        with _FleetStub(str(_ROOT / "fleet.py")) as stub:
            saved = os.environ.get("AGENT_FLEET_SUITE_ROOT")
            os.environ["AGENT_FLEET_SUITE_ROOT"] = stub
            try:
                with self.assertRaises(TransportError):
                    resolve_fleet_py()
            finally:
                if saved is None:
                    os.environ.pop("AGENT_FLEET_SUITE_ROOT", None)
                else:
                    os.environ["AGENT_FLEET_SUITE_ROOT"] = saved

    def test_unattested_directory_is_not_accepted(self):
        with _FleetStubDirectory(_ROOT) as suite_root:
            saved = os.environ.get("AGENT_FLEET_SUITE_ROOT")
            os.environ["AGENT_FLEET_SUITE_ROOT"] = str(suite_root)
            try:
                with self.assertRaises(TransportError):
                    resolve_fleet_py()
            finally:
                if saved is None:
                    os.environ.pop("AGENT_FLEET_SUITE_ROOT", None)
                else:
                    os.environ["AGENT_FLEET_SUITE_ROOT"] = saved


def _fleet_envelope_payload(*, status="DRY_RUN", final=None):
    """Build a minimal Fleet JSON object for unit tests."""
    payload = {
        "action": "run",
        "adapter": "grok",
        "agent": "grok",
        "attempt_id": "attempt-fleet-7f2c",
        "command": ["C:/grok.exe", "--cwd", "ws"],
        "model": "grok-4.6",
        "status": status,
        "stdin": "ECHO-OF-PROMPT-MUST-NOT-LEAK",
    }
    if final is not None:
        payload["final"] = final
    return payload


class _FleetStub:
    """Create a fake ``fleet.py`` file at ``path`` for the duration of a block."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.path).write_text("# stub fleet.py for tests\n",
                                   encoding="utf-8")
        return self.path

    def __exit__(self, exc_type, exc, tb):
        try:
            Path(self.path).unlink()
        except OSError:
            pass


class _FleetStubDirectory:
    """Create a directory layout ``<root>/agent-fleet/scripts/fleet.py``."""

    def __init__(self, parent: Path):
        self.parent = parent

    def __enter__(self):
        root = self.parent / ".tmp-fleet-suite"
        target = root / "agent-fleet" / "scripts" / "fleet.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub fleet.py for tests\n", encoding="utf-8")
        return root

    def __exit__(self, exc_type, exc, tb):
        import shutil
        try:
            shutil.rmtree(self.parent / ".tmp-fleet-suite")
        except OSError:
            pass


class FleetCurateWireTests(unittest.TestCase):
    """``_build_fleet_reviewers`` produces the right spec for production."""

    def test_production_spec_pins_python_and_fleet_placeholders(self):
        from curate_once import _build_fleet_reviewers

        class _Cfg:
            publication = type(
                "P", (), {
                    "reviewer": type(
                        "R", (), {
                            "mode": "fleet",
                            "candidates": [
                                type("C", (), {"agent": "grok",
                                               "model": "grok-4.6"})(),
                            ],
                        })(),
                })()

        reviewers = _build_fleet_reviewers(_Cfg())
        self.assertEqual(len(reviewers), 1)
        spec = reviewers[0]["spec"]
        self.assertEqual(spec.executable, sys.executable)
        self.assertIn("${fleet_py}", spec.argv)
        self.assertIn("run", spec.argv)
        self.assertIn("--task-file", spec.argv)
        self.assertIn("${task_file}", spec.argv)
        self.assertIn("--agent", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--agent") + 1], "grok")
        self.assertIn("--model", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--model") + 1], "grok-4.6")
        # The old broken artefact must not appear anywhere.
        joined = " ".join(spec.argv)
        self.assertNotIn("agent-fleet.cmd", joined)
        # The 2026-09-20 probe did not validate --mode/--kind/--complexity/
        # --timeout/--workspace/--assessment-file in a real Fleet run, so
        # the production spec keeps only the three validated flags.
        # Compare exact argv TOKENS: '--mode' is a substring of '--model',
        # so matching against the joined string false-positives here.
        for absent in ("--mode", "--kind", "--complexity", "--timeout",
                       "--workspace", "--assessment-file"):
            self.assertNotIn(absent, list(spec.argv),
                             f"unvalidated flag in spec: {absent}")


if __name__ == "__main__":
    unittest.main()
