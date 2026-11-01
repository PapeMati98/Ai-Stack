from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ollama_local_runtime


class OllamaLocalRuntimeTests(unittest.TestCase):
    def config(self, root: Path) -> dict[str, object]:
        executable = root / "ollama.exe"
        executable.write_bytes(b"ollama-test")
        return {
            "kind": "ollama-windows-external",
            "state_id": "qwen3.8-abliterated-ollama-11439",
            "base_url": "http://127.0.0.1:11439/v1",
            "native_tags_url": "http://127.0.0.1:11439/api/tags",
            "served_model_name": ollama_local_runtime.PINNED_QWEN_MODEL,
            "model_manifest_digest": "b" * 64,
            "ollama_executable_windows": str(executable),
            "ollama_executable_sha256": "a" * 64,
            "ollama_version": "0.33.2",
            "context_window_tokens": 16384,
            "max_num_seqs": 1,
            "max_wall_time_seconds": 600,
            "externally_managed": True,
            "provider_cost_pln": 0.0,
            "provider_cost_display": "0 zł",
        }

    def test_validate_pins_loopback_capacity_context_and_zero_provider_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ollama_local_runtime.validate_config(
                self.config(Path(temporary))
            )
        self.assertEqual(config["port"], 11439)
        self.assertEqual(config["context_window_tokens"], 16384)
        self.assertEqual(config["max_num_seqs"], 1)
        self.assertEqual(config["provider_cost_display"], "0 zł")

    def test_validate_rejects_lan_listener_and_parallel_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            config["base_url"] = "http://0.0.0.0:11439/v1"
            with self.assertRaisesRegex(
                ollama_local_runtime.LocalRuntimeError, "127.0.0.1"
            ):
                ollama_local_runtime.validate_config(config)
            config = self.config(Path(temporary))
            config["max_num_seqs"] = 2
            with self.assertRaisesRegex(
                ollama_local_runtime.LocalRuntimeError, "exactly one sequence"
            ):
                ollama_local_runtime.validate_config(config)

    def test_pinned_contract_rejects_any_noncanonical_runtime_field(self) -> None:
        expected_path = Path(r"C:\\pinned\\ollama.exe")
        normalized = {
            "base_url": ollama_local_runtime.PINNED_OLLAMA_BASE_URL,
            "native_tags_url": ollama_local_runtime.PINNED_OLLAMA_TAGS_URL,
            "served_model_name": ollama_local_runtime.PINNED_QWEN_MODEL,
            "model_manifest_digest": (
                ollama_local_runtime.PINNED_QWEN_MANIFEST_DIGEST
            ),
            "ollama_executable_windows": str(expected_path),
            "ollama_executable_sha256": (
                ollama_local_runtime.PINNED_OLLAMA_EXECUTABLE_SHA256
            ),
            "ollama_version": ollama_local_runtime.PINNED_OLLAMA_VERSION,
            "context_window_tokens": 16384,
            "max_num_seqs": 1,
            "max_wall_time_seconds": 600,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
        }
        with (
            mock.patch.object(
                ollama_local_runtime,
                "validate_config",
                return_value=normalized,
            ),
            mock.patch.object(
                ollama_local_runtime,
                "_expanded_windows_path",
                return_value=expected_path,
            ),
        ):
            self.assertEqual(
                ollama_local_runtime.validate_pinned_qwen_ollama_contract({}),
                normalized,
            )
            mismatched = dict(normalized)
            mismatched["model_manifest_digest"] = "a" * 64
            with (
                mock.patch.object(
                    ollama_local_runtime,
                    "validate_config",
                    return_value=mismatched,
                ),
                self.assertRaisesRegex(
                    ollama_local_runtime.LocalRuntimeError,
                    "model_manifest_digest",
                ),
            ):
                ollama_local_runtime.validate_pinned_qwen_ollama_contract({})

    def test_runtime_ready_requires_endpoint_listener_and_executable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            with (
                mock.patch.object(
                    ollama_local_runtime,
                    "validate_pinned_qwen_ollama_contract",
                    return_value=ollama_local_runtime.validate_config(config),
                ),
                mock.patch.object(
                    ollama_local_runtime,
                    "_endpoint_identity",
                    return_value={"matches": True},
                ),
                mock.patch.object(
                    ollama_local_runtime,
                    "_listener_identity",
                    return_value={"matches": True},
                ),
                mock.patch.object(
                    ollama_local_runtime,
                    "_executable_identity",
                    return_value={"matches": True},
                ),
            ):
                status = ollama_local_runtime.runtime_status(config)
        self.assertEqual(status["status"], "READY")
        self.assertTrue(status["routing_ready"])
        self.assertTrue(status["externally_managed"])

    def test_read_json_url_bypasses_ambient_proxies(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        opener = mock.Mock()
        opener.open.return_value = response
        proxy_handler = object()
        with (
            mock.patch.object(
                ollama_local_runtime.urllib.request,
                "ProxyHandler",
                return_value=proxy_handler,
            ) as proxy_handler_factory,
            mock.patch.object(
                ollama_local_runtime.urllib.request,
                "build_opener",
                return_value=opener,
            ) as build_opener,
        ):
            payload = ollama_local_runtime._read_json_url(
                "http://127.0.0.1:11439/api/tags", timeout=3.0
            )
        self.assertEqual(payload, {"ok": True})
        proxy_handler_factory.assert_called_once_with({})
        build_opener.assert_called_once_with(proxy_handler)
        opener.open.assert_called_once_with(
            "http://127.0.0.1:11439/api/tags", timeout=3.0
        )

    def test_executable_identity_rejects_hash_before_version_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            with (
                mock.patch.object(
                    ollama_local_runtime, "_sha256", return_value="c" * 64
                ),
                mock.patch.object(ollama_local_runtime.subprocess, "run") as run,
            ):
                identity = ollama_local_runtime._executable_identity(config)
        self.assertFalse(identity["matches"])
        self.assertIsNone(identity["version"])
        run.assert_not_called()

    def test_executable_identity_rehashes_after_version_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            config["ollama_executable_sha256"] = "c" * 64
            completed = SimpleNamespace(
                returncode=0,
                stdout="ollama version 0.33.2",
                stderr="",
            )
            with (
                mock.patch.object(
                    ollama_local_runtime,
                    "_sha256",
                    side_effect=["c" * 64, "c" * 64],
                ) as sha256,
                mock.patch.object(
                    ollama_local_runtime.subprocess,
                    "run",
                    return_value=completed,
                ),
            ):
                identity = ollama_local_runtime._executable_identity(config)
        self.assertTrue(identity["matches"])
        self.assertEqual(sha256.call_count, 2)

    def test_endpoint_identity_requires_actual_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            expected = str(config["model_manifest_digest"])
            with mock.patch.object(
                ollama_local_runtime,
                "_read_json_url",
                side_effect=[
                    {
                        "models": [
                            {
                                "name": (
                                    ollama_local_runtime.PINNED_QWEN_MODEL
                                    + ":latest"
                                ),
                                "digest": "sha256:" + expected,
                            }
                        ]
                    },
                    {"data": [{"id": ollama_local_runtime.PINNED_QWEN_MODEL}]},
                ],
            ):
                identity = ollama_local_runtime._endpoint_identity(config)
        self.assertTrue(identity["matches"])
        self.assertEqual(identity["matching_openai_models"], [
            ollama_local_runtime.PINNED_QWEN_MODEL
        ])
        self.assertEqual(
            identity["native_model_digests"][0]["manifest_digest"], expected
        )

    def test_endpoint_identity_fails_closed_on_tag_only_or_wrong_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            expected = str(config["model_manifest_digest"])
            with mock.patch.object(
                ollama_local_runtime,
                "_read_json_url",
                side_effect=[
                    {
                        "models": [
                            {
                                "name": ollama_local_runtime.PINNED_QWEN_MODEL,
                                "digest": "sha256:" + expected,
                            }
                        ]
                    },
                    {"data": []},
                ],
            ):
                tag_only = ollama_local_runtime._endpoint_identity(config)
            self.assertFalse(tag_only["matches"])
            with mock.patch.object(
                ollama_local_runtime,
                "_read_json_url",
                side_effect=[
                    {
                        "models": [
                            {
                                "name": ollama_local_runtime.PINNED_QWEN_MODEL,
                                "digest": "sha256:" + "c" * 64,
                            }
                        ]
                    },
                    {"data": [{"id": ollama_local_runtime.PINNED_QWEN_MODEL}]},
                ],
            ):
                identity = ollama_local_runtime._endpoint_identity(config)
        self.assertFalse(identity["matches"])

    def test_ensure_runtime_never_starts_an_unready_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            with mock.patch.object(
                ollama_local_runtime,
                "runtime_status",
                return_value={"routing_ready": False},
            ):
                with self.assertRaisesRegex(
                    ollama_local_runtime.LocalRuntimeError,
                    "Fleet will not start",
                ):
                    ollama_local_runtime.ensure_runtime(config)

    def test_execution_contract_is_single_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            with ollama_local_runtime.execution_lock(
                config, timeout=1, state_root=root / "state"
            ) as slot:
                self.assertEqual(slot["max_num_seqs"], 1)
                self.assertEqual(slot["slot_index"], 0)
            with self.assertRaisesRegex(
                ollama_local_runtime.LocalRuntimeError, "capacity must be 1"
            ):
                with ollama_local_runtime.execution_lock(
                    config,
                    timeout=1,
                    capacity=2,
                    state_root=root / "state",
                ):
                    pass


if __name__ == "__main__":
    unittest.main()
