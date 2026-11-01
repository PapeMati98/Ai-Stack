from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import qwen_local_runtime  # noqa: E402


class QwenLocalRuntimeTests(unittest.TestCase):
    def config(self, model_dir: Path) -> dict[str, object]:
        return {
            "kind": "vllm-wsl",
            "state_id": "qwen3.8-27b-local-500k",
            "distribution": "Ubuntu",
            "base_url": "http://127.0.0.1:8018/v1",
            "served_model_name": "qwen3.8-27b-local-500k",
            "model_path_windows": str(model_dir),
            "model_path_wsl": "/mnt/e/models/Qwen3.8-27B-BF16",
            "vllm_executable_wsl": "/opt/agent-fleet/qwen38-vllm/bin/vllm",
            "cuda_home_wsl": "/opt/agent-fleet/qwen38-vllm/lib/python3.12/site-packages/nvidia/cu13",
            "cuda_toolkit_version": "13.0",
            "systemd_unit": "agent-fleet-qwen38-local.service",
            "native_context_window_tokens": 262144,
            "context_window_tokens": 524288,
            "yarn_factor": 2.0,
            "gpu_index": 0,
            "gpu_memory_utilization": 0.95,
            "kv_cache_dtype": "fp8",
            "max_num_seqs": 2,
            "max_num_batched_tokens": 8192,
            "startup_timeout_seconds": 900,
            "provider_cost_pln": 0.0,
            "provider_cost_display": "0 zł",
        }

    def test_serve_command_pins_context_gpu_backend_and_two_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = qwen_local_runtime.validate_config(self.config(model_dir))
            command = qwen_local_runtime.linux_serve_args(config)
        self.assertEqual(command[command.index("--max-model-len") + 1], "524288")
        self.assertEqual(command[command.index("--max-num-seqs") + 1], "2")
        self.assertEqual(command[command.index("--kv-cache-dtype") + 1], "fp8")
        self.assertEqual(
            command[command.index("--attention-backend") + 1], "FLASHINFER"
        )
        self.assertIn("CUDA_VISIBLE_DEVICES=0", command)
        self.assertIn(
            "PATH=/opt/agent-fleet/qwen38-vllm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            command,
        )
        self.assertIn(
            "CUDA_HOME=/opt/agent-fleet/qwen38-vllm/lib/python3.12/site-packages/nvidia/cu13",
            command,
        )
        override = json.loads(command[command.index("--hf-overrides") + 1])
        self.assertEqual(override["text_config"]["rope_parameters"]["factor"], 2.0)

    def test_detached_command_redirects_inside_wsl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = qwen_local_runtime.validate_config(self.config(model_dir))
            log_path = Path(temporary) / "vllm log.txt"
            command = qwen_local_runtime.serve_command(config, log_path=log_path)
        self.assertIn("systemd-run", command)
        self.assertIn("--unit=agent-fleet-qwen38-local.service", command)
        self.assertIn("--property=Restart=on-failure", command)
        self.assertIn("--property=RestartSec=5s", command)
        self.assertEqual(command[-3:-1], ["/usr/bin/bash", "-lc"])
        self.assertIn("exec env", command[-1])
        self.assertIn("2>&1", command[-1])
        self.assertIn("vllm log.txt", command[-1])

    def test_sandbox_base_url_is_derived_from_verified_loopback_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            url = qwen_local_runtime.sandbox_base_url(self.config(model_dir))
        self.assertEqual(url, "http://host.docker.internal:8018/v1")

    def test_validation_rejects_remote_endpoint_and_nonzero_provider_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            config["base_url"] = "https://example.com/v1"
            with self.assertRaisesRegex(
                qwen_local_runtime.LocalRuntimeError, "loopback"
            ):
                qwen_local_runtime.validate_config(config)
            config = self.config(model_dir)
            config["provider_cost_pln"] = 0.01
            with self.assertRaisesRegex(
                qwen_local_runtime.LocalRuntimeError, "exactly 0 PLN"
            ):
                qwen_local_runtime.validate_config(config)

            config = self.config(model_dir)
            config["max_num_seqs"] = 3
            with self.assertRaisesRegex(
                qwen_local_runtime.LocalRuntimeError, "supports max_num_seqs=1 or 2"
            ):
                qwen_local_runtime.validate_config(config)

    @mock.patch.object(qwen_local_runtime, "_execution_slot_lock")
    def test_execution_lock_uses_two_dedicated_per_model_slots(
        self, slot_lock: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            slot_lock.return_value.__enter__.return_value = 1
            with qwen_local_runtime.execution_lock(
                self.config(model_dir), timeout=60, state_root=root / "state"
            ) as slot:
                self.assertEqual(slot["state_id"], "qwen3.8-27b-local-500k")
                self.assertEqual(slot["max_num_seqs"], 2)
                self.assertEqual(slot["runtime_max_num_seqs"], 2)
                self.assertEqual(slot["slot_index"], 1)
            expected = (
                root
                / "state"
                / qwen_local_runtime.capacity_lock_identity(
                    qwen_local_runtime.validate_config(self.config(model_dir))
                )
                / "execution.lock"
            )
            slot_lock.assert_called_once_with(expected, 2, 60)

    def test_execution_lock_rejects_capacity_above_runtime_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                qwen_local_runtime.LocalRuntimeError, "Execution capacity"
            ):
                with qwen_local_runtime.execution_lock(
                    self.config(model_dir),
                    timeout=1,
                    capacity=3,
                    state_root=root / "state",
                ):
                    pass

    def test_active_maintenance_lease_removes_runtime_from_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "_all_execution_slots_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "endpoint_status",
                    return_value={"status": "READY"},
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "prerequisite_status",
                    return_value={"ready_to_start": True},
                ),
            ):
                lease = qwen_local_runtime.acquire_maintenance(
                    config,
                    owner="synaps-gpu-smoke-v2",
                    reason="exclusive-gpu0",
                    ttl_seconds=5400,
                    lease_id="qwen-maint-test-active",
                    state_root=root / "state",
                )
                status = qwen_local_runtime.runtime_status(
                    config, state_root=root / "state"
                )
        self.assertTrue(lease["active"])
        self.assertEqual(status["status"], "MAINTENANCE")
        self.assertFalse(status["routing_ready"])
        self.assertEqual(
            status["maintenance"]["lease_id"], "qwen-maint-test-active"
        )

    def test_execution_lock_refuses_active_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "_all_execution_slots_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
            ):
                qwen_local_runtime.acquire_maintenance(
                    config,
                    owner="synaps-gpu-smoke-v2",
                    reason="exclusive-gpu0",
                    ttl_seconds=5400,
                    lease_id="qwen-maint-test-lock",
                    state_root=root / "state",
                )
            with self.assertRaisesRegex(
                qwen_local_runtime.LocalRuntimeError,
                "temporarily unavailable",
            ):
                with qwen_local_runtime.execution_lock(
                    config, timeout=1, state_root=root / "state"
                ):
                    pass

    def test_ensure_requires_the_active_maintenance_lease_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "_all_execution_slots_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
            ):
                qwen_local_runtime.acquire_maintenance(
                    config,
                    owner="synaps-gpu-smoke-v2",
                    reason="exclusive-gpu0",
                    ttl_seconds=5400,
                    lease_id="qwen-maint-test-ensure",
                    state_root=root / "state",
                )
                with self.assertRaisesRegex(
                    qwen_local_runtime.LocalRuntimeError,
                    "temporarily unavailable",
                ):
                    qwen_local_runtime.ensure_runtime(
                        config, state_root=root / "state"
                    )
                with mock.patch.object(
                    qwen_local_runtime,
                    "endpoint_status",
                    return_value={"status": "READY"},
                ):
                    ensured = qwen_local_runtime.ensure_runtime(
                        config,
                        state_root=root / "state",
                        maintenance_lease_id="qwen-maint-test-ensure",
                    )
        self.assertEqual(ensured["status"], "READY")
        self.assertFalse(ensured["started"])

    def test_expired_maintenance_lease_is_not_a_permanent_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = qwen_local_runtime.validate_config(self.config(model_dir))
            lease_path = (
                root
                / "state"
                / "qwen3.8-27b-local-500k"
                / "maintenance.json"
            )
            qwen_local_runtime._atomic_json(
                lease_path,
                {
                    "schema_version": 1,
                    "state_id": "qwen3.8-27b-local-500k",
                    "lease_id": "qwen-maint-test-expired",
                    "owner": "synaps-gpu-smoke-v2",
                    "reason": "exclusive-gpu0",
                    "acquired_at_unix": 100.0,
                    "expires_at_unix": 200.0,
                    "ttl_seconds": 100,
                },
            )
            status = qwen_local_runtime.maintenance_status(
                config, state_root=root / "state", now_unix=201.0
            )
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "endpoint_status",
                    return_value={"status": "STOPPED"},
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "prerequisite_status",
                    return_value={"ready_to_start": True},
                ),
                mock.patch.object(qwen_local_runtime.time, "time", return_value=201.0),
            ):
                runtime = qwen_local_runtime.runtime_status(
                    config, state_root=root / "state"
                )
        self.assertEqual(status["status"], "EXPIRED")
        self.assertFalse(status["active"])
        self.assertTrue(runtime["routing_ready"])

    def test_release_requires_ready_endpoint_and_matching_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "_all_execution_slots_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
            ):
                qwen_local_runtime.acquire_maintenance(
                    config,
                    owner="synaps-gpu-smoke-v2",
                    reason="exclusive-gpu0",
                    ttl_seconds=5400,
                    lease_id="qwen-maint-test-release",
                    state_root=root / "state",
                )
                with mock.patch.object(
                    qwen_local_runtime,
                    "endpoint_status",
                    return_value={"status": "STOPPED"},
                ):
                    with self.assertRaisesRegex(
                        qwen_local_runtime.LocalRuntimeError,
                        "before the exact local Qwen endpoint is READY",
                    ):
                        qwen_local_runtime.release_maintenance(
                            config,
                            lease_id="qwen-maint-test-release",
                            state_root=root / "state",
                        )
                self.assertTrue(
                    qwen_local_runtime.maintenance_status(
                        config, state_root=root / "state"
                    )["active"]
                )
                with mock.patch.object(
                    qwen_local_runtime,
                    "endpoint_status",
                    return_value={
                        "status": "READY",
                        "observed_models": ["qwen3.8-27b-local-500k"],
                    },
                ):
                    released = qwen_local_runtime.release_maintenance(
                        config,
                        lease_id="qwen-maint-test-release",
                        state_root=root / "state",
                    )
            self.assertEqual(released["status"], "RELEASED")
            self.assertFalse(
                qwen_local_runtime.maintenance_status(
                    config, state_root=root / "state"
                )["active"]
            )

    def test_corrupt_lease_can_only_be_quarantined_after_exact_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            lease_path = (
                root
                / "state"
                / "qwen3.8-27b-local-500k"
                / "maintenance.json"
            )
            lease_path.parent.mkdir(parents=True)
            lease_path.write_text("not-json\n", encoding="utf-8")
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "endpoint_status",
                    return_value={"status": "STOPPED"},
                ),
            ):
                with self.assertRaisesRegex(
                    qwen_local_runtime.LocalRuntimeError,
                    "before the exact local Qwen endpoint is READY",
                ):
                    qwen_local_runtime.force_clear_corrupt_maintenance(
                        config, state_root=root / "state"
                    )
            self.assertTrue(lease_path.is_file())
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "endpoint_status",
                    return_value={
                        "status": "READY",
                        "observed_models": ["qwen3.8-27b-local-500k"],
                    },
                ),
            ):
                result = qwen_local_runtime.force_clear_corrupt_maintenance(
                    config, state_root=root / "state"
                )
            self.assertEqual(result["status"], "CORRUPT_LEASE_QUARANTINED")
            self.assertFalse(lease_path.exists())
            self.assertTrue(Path(result["quarantine_path"]).is_file())
            self.assertEqual(
                qwen_local_runtime.maintenance_status(
                    config, state_root=root / "state"
                )["status"],
                "INACTIVE",
            )

    def test_force_clear_refuses_valid_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "_all_execution_slots_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
            ):
                qwen_local_runtime.acquire_maintenance(
                    config,
                    owner="synaps-gpu-smoke-v2",
                    reason="exclusive-gpu0",
                    ttl_seconds=5400,
                    lease_id="qwen-maint-valid-force-clear",
                    state_root=root / "state",
                )
                with self.assertRaisesRegex(
                    qwen_local_runtime.LocalRuntimeError,
                    "Maintenance lease is valid",
                ):
                    qwen_local_runtime.force_clear_corrupt_maintenance(
                        config, state_root=root / "state"
                    )

    def test_stop_is_serialized_and_rejects_expired_lease_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = qwen_local_runtime.validate_config(self.config(model_dir))
            lease_path = (
                root
                / "state"
                / "qwen3.8-27b-local-500k"
                / "maintenance.json"
            )
            lease_path.parent.mkdir(parents=True)
            lease_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_id": config["state_id"],
                        "lease_id": "qwen-maint-expired-stop",
                        "owner": "synaps-gpu-smoke-v2",
                        "reason": "exclusive-gpu0",
                        "acquired_at_unix": 100.0,
                        "expires_at_unix": 200.0,
                        "ttl_seconds": 100,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ) as startup_lock,
                mock.patch.object(qwen_local_runtime.time, "time", return_value=300.0),
            ):
                with self.assertRaisesRegex(
                    qwen_local_runtime.LocalRuntimeError,
                    "stale lease-authorized stop",
                ):
                    qwen_local_runtime.stop_runtime(
                        config,
                        state_root=root / "state",
                        maintenance_lease_id="qwen-maint-expired-stop",
                    )
            startup_lock.assert_called_once()

    def test_failed_drain_removes_new_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = self.config(model_dir)
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "_all_execution_slots_lock",
                    side_effect=qwen_local_runtime.LocalRuntimeError(
                        "Timed out draining local Qwen execution slots"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    qwen_local_runtime.LocalRuntimeError, "Timed out draining"
                ):
                    qwen_local_runtime.acquire_maintenance(
                        config,
                        owner="synaps-gpu-smoke-v2",
                        reason="exclusive-gpu0",
                        ttl_seconds=5400,
                        lease_id="qwen-maint-test-drain",
                        state_root=root / "state",
                    )
            self.assertEqual(
                qwen_local_runtime.maintenance_status(
                    config, state_root=root / "state"
                )["status"],
                "INACTIVE",
            )

    def test_retry_of_undrained_lease_finishes_the_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
            config = qwen_local_runtime.validate_config(self.config(model_dir))
            lease_path = (
                root
                / "state"
                / "qwen3.8-27b-local-500k"
                / "maintenance.json"
            )
            now = time.time()
            qwen_local_runtime._atomic_json(
                lease_path,
                {
                    "schema_version": 1,
                    "state_id": "qwen3.8-27b-local-500k",
                    "lease_id": "qwen-maint-test-retry",
                    "owner": "synaps-gpu-smoke-v2",
                    "reason": "exclusive-gpu0",
                    "acquired_at_unix": now,
                    "expires_at_unix": now + 5400,
                    "ttl_seconds": 5400,
                },
            )
            with (
                mock.patch.object(
                    qwen_local_runtime,
                    "_startup_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                mock.patch.object(
                    qwen_local_runtime,
                    "_all_execution_slots_lock",
                    side_effect=lambda *_args: nullcontext(),
                ) as drain,
            ):
                retried = qwen_local_runtime.acquire_maintenance(
                    config,
                    owner="synaps-gpu-smoke-v2",
                    reason="exclusive-gpu0",
                    ttl_seconds=5400,
                    lease_id="qwen-maint-test-retry",
                    state_root=root / "state",
                )
        drain.assert_called_once()
        self.assertTrue(retried["active"])
        self.assertIsInstance(retried.get("drained_at_unix"), float)

    def test_two_processes_hold_distinct_slots_and_third_waiter_times_out(self) -> None:
        script = (
            "import sys,time; from pathlib import Path; "
            "from qwen_local_runtime import _execution_slot_lock; "
            "cm=_execution_slot_lock(Path(sys.argv[1]),2,5); "
            "slot=cm.__enter__(); print(slot,flush=True); "
            "time.sleep(float(sys.argv[2])); cm.__exit__(None,None,None)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "execution.lock"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(SCRIPT_DIR)
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(lock_path), "2"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                for _ in range(2)
            ]
            try:
                held_slots = {
                    int(process.stdout.readline().strip()) for process in processes
                }
                self.assertEqual(held_slots, {0, 1})
                started = time.monotonic()
                with self.assertRaisesRegex(
                    qwen_local_runtime.LocalRuntimeError, "Timed out waiting"
                ):
                    with qwen_local_runtime._execution_slot_lock(
                        lock_path, 2, 1
                    ):
                        pass
                self.assertGreaterEqual(time.monotonic() - started, 0.9)
            finally:
                for process in processes:
                    _, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stderr)

            with qwen_local_runtime._execution_slot_lock(lock_path, 2, 1) as slot:
                self.assertIn(slot, {0, 1})

    def test_maintenance_drain_holds_every_execution_slot(self) -> None:
        script = (
            "import sys,time; from pathlib import Path; "
            "from qwen_local_runtime import _all_execution_slots_lock; "
            "cm=_all_execution_slots_lock(Path(sys.argv[1]),2,5); "
            "cm.__enter__(); print('drained',flush=True); "
            "time.sleep(2); cm.__exit__(None,None,None)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "execution.lock"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(SCRIPT_DIR)
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "drained")
                with self.assertRaisesRegex(
                    qwen_local_runtime.LocalRuntimeError, "Timed out waiting"
                ):
                    with qwen_local_runtime._execution_slot_lock(
                        lock_path, 2, 1
                    ):
                        pass
            finally:
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
