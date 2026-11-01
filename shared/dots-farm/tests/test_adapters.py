"""Adapter contract tests. Fixtures retained under DOTS_TEST_ROOT; no cleanup."""

import copy
import json
import os
import sqlite3
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
)

from dots_farm import adapters
from dots_farm.adapters import (
    OpenRouterKeyManager,
    OpenRouterModel,
    SyntheticModel,
    SyntheticOffice,
    SyntheticSource,
    SyntheticTarget,
    _TargetTimeoutAfterWrite,
    validate_decision,
)
from dots_farm.hermes import profile_plan, supervisor_plan


def fixture_root():
    base = os.environ.get(
        "DOTS_TEST_ROOT",
        os.path.join(os.path.dirname(__file__), "..", ".test-runs"),
    )
    root = os.path.join(os.path.normpath(base), uuid.uuid4().hex)
    os.makedirs(root, exist_ok=True)
    return root


class DecisionTests(unittest.TestCase):
    def test_malformed_decisions_rejected(self):
        with self.assertRaises(ValueError):
            validate_decision({"action": "fix-it", "reason_code": "ok"}, "observer")
        with self.assertRaises(ValueError):
            validate_decision(
                {"action": "alert", "reason_code": "ok", "tool": "exec"}, "observer"
            )
        with self.assertRaises(ValueError):
            validate_decision(
                {"action": "alert", "reason_code": "ok", "script": "run"}, "observer"
            )
        with self.assertRaises(ValueError):
            validate_decision(
                {"action": "alert", "reason_code": "ok", "authority": "root"},
                "observer",
            )
        with self.assertRaises(ValueError):
            validate_decision({"action": "collect", "reason_code": "ok"}, "observer")
        with self.assertRaises(ValueError):
            validate_decision({"action": "alert", "reason_code": "ok"}, "collector")
        with self.assertRaises(ValueError):
            validate_decision("alert", "observer")
        with self.assertRaises(ValueError):
            validate_decision(
                {"action": "alert", "reason_code": "BAD CODE!"}, "observer"
            )

    def test_kind_matrix_ok(self):
        self.assertEqual(
            validate_decision({"action": "alert", "reason_code": "noise"}, "observer")[
                "action"
            ],
            "alert",
        )
        self.assertEqual(
            validate_decision(
                {"action": "collect", "reason_code": "inv-1"}, "collector"
            )["action"],
            "collect",
        )


class SyntheticFlowTests(unittest.TestCase):
    def test_source_caps_and_org_boundary(self):
        root = fixture_root()
        path = os.path.join(root, "source.json")
        items = [
            {"id": "a%d" % i, "org": "acme", "content": "hello",
             "model_decision": {"action": "ignore", "reason_code": "noise"}}
            for i in range(3)
        ]
        items.append(
            {"id": "x1", "org": "other", "content": "hello",
             "model_decision": {"action": "ignore", "reason_code": "noise"}}
        )
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(items, fh)
        src = SyntheticSource(path)
        got = src.poll({"org": "acme", "max_queue": 3, "max_payload_bytes": 16384})
        self.assertEqual(len(got), 3)
        self.assertTrue(all(e["org"] == "acme" for e in got))

    def test_source_failure_injection(self):
        root = fixture_root()
        path = os.path.join(root, "failed.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"status": "failed", "items": []}, fh)
        from dots_farm.adapters import _SourceFailed

        with self.assertRaises(_SourceFailed):
            SyntheticSource(path).poll({"org": "acme"})
        self.assertTrue(os.path.exists(path))  # fixture retained

    def test_target_idempotency_after_uncertain_write(self):
        root = fixture_root()
        path = os.path.join(root, "target.db")
        target = SyntheticTarget(path)
        payload = {
            "kind": "invoice-draft",
            "invoice": {"org": "acme", "number": "7", "date": "2026-10-01",
                        "amount": "10.00", "currency": "USD"},
            "simulate_timeout_after_write": True,
        }
        with self.assertRaises(_TargetTimeoutAfterWrite):
            target.put("eff-1", payload)
        # Uncertain write reconciled via lookup, never blind reissue.
        found = target.lookup("eff-1")
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "confirmed")
        retry = dict(payload)
        retry.pop("simulate_timeout_after_write")
        second = target.put("eff-1", retry)
        self.assertEqual(second["receipt"], found["receipt"])
        # Duplicate invoice from a second collector yields one record.
        dup = target.put("eff-2", dict(retry))
        self.assertEqual(dup["receipt"], found["receipt"])
        self.assertEqual(target.count(), 1)

    def test_office_rejects_content_and_enforces_bounds(self):
        root = fixture_root()
        office = SyntheticOffice(os.path.join(root, "office.db"))
        base = {
            "event_id": "e1", "dot_id": "dot1", "org": "acme", "project": "p1",
            "kind": "alert", "reason_code": "noise", "opaque_ref": "r1",
            "hops": 1, "expires_at": 1790000000.0,
        }
        office.send(dict(base))
        self.assertEqual(office.count(), 1)
        office.send(dict(base))  # dedupe
        self.assertEqual(office.count(), 1)
        bad = dict(base)
        bad["event_id"] = "e2"
        bad["content"] = "secret mail body"
        with self.assertRaises(ValueError):
            office.send(bad)
        bad2 = dict(base)
        bad2["event_id"] = "e3"
        bad2["hops"] = 5
        with self.assertRaises(ValueError):
            office.send(bad2)
        for key in ("event_id", "org", "project", "kind", "opaque_ref"):
            free = dict(base)
            free["event_id"] = "free-%s" % key
            free[key] = "Free text with spaces / PII-like body!"
            with self.assertRaises(ValueError):
                office.send(free)

    def test_synthetic_model_shape(self):
        out = SyntheticModel().decide(
            {"kind": "observer", "org": "acme"},
            {"model_decision": {"action": "ignore", "reason_code": "noise"}},
            [],
        )
        self.assertEqual(out["decision"]["action"], "ignore")
        self.assertIsInstance(out["cost_micro_usd"], int)
        self.assertIn("input_tokens", out["usage"])


class LiveTransportTests(unittest.TestCase):
    def _live_config(self, **over):
        cfg = {
            "live_enabled": True,
            "approved_budget_micro_usd": 1000,
            "model": "qwen-test-1",
            "qualified_models": ["qwen-test-1"],
            "qualified_provider": "prov-a",
            "provider_order": ["prov-a"],
            "secret_reference": "ref-1",
            "max_tokens": 16,
            "tariff_per_token_micro": {"input": "1", "output": "2"},
        }
        cfg.update(over)
        return cfg

    def test_live_blocked_without_grant(self):
        model = OpenRouterModel(
            {"live_enabled": False}, lambda ref: "s3cr3t", transport=lambda *a: {}
        )
        with self.assertRaises(PermissionError):
            model.decide({"kind": "observer", "org": "acme"}, {"content": "hi"}, [])

    def test_forbidden_model_denied(self):
        cfg = self._live_config(
            model="muse-spark-1.2", qualified_models=["muse-spark-1.2"]
        )
        model = OpenRouterModel(cfg, lambda ref: "s3cr3t", transport=lambda *a: {})
        with self.assertRaises(PermissionError):
            model.decide({"kind": "observer", "org": "acme"}, {"content": "hi"}, [])

    def test_no_retry_fallback_redirect_and_secret_redaction(self):
        calls = []

        def transport(url, headers, body, timeout, max_bytes):
            calls.append((url, dict(body)))
            self.assertEqual(
                url, "https://openrouter.ai/api/v1/chat/completions"
            )
            self.assertEqual(body["provider"]["only"], ["prov-a"])
            self.assertFalse(body["provider"]["allow_fallbacks"])
            self.assertNotIn("tools", body)
            self.assertEqual(body["provider"]["data_collection"], "deny")
            self.assertIs(body["provider"]["require_parameters"], True)
            self.assertNotIn("zdr", body["provider"])
            self.assertEqual(body["reasoning"], {"enabled": False})
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertEqual(
                set(body["response_format"]["json_schema"]["schema"]["required"]),
                {"action", "reason_code"},
            )
            return {
                "model": "qwen-test-1",
                "provider": "prov-a",
                "choices": [{"message": {"content": json.dumps(
                    {"action": "ignore", "reason_code": "noise"})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "cost": "0.00002"},
            }

        model = OpenRouterModel(self._live_config(), lambda ref: "s3cr3t",
                                transport=transport)
        out = model.decide({"kind": "observer", "org": "acme"}, {"content": "hello"}, [])
        self.assertEqual(len(calls), 1)  # exactly one attempt, no retry
        self.assertEqual(out["decision"]["action"], "ignore")
        self.assertEqual(out["cost_micro_usd"], 20)

        def redirect_transport(url, headers, body, timeout, max_bytes):
            return {"_redirect": True, "model": "qwen-test-1",
                    "Location": "https://evil.example/"}

        redirect_cases = [
            {"_redirect": True, "model": "qwen-test-1",
             "Location": "https://evil.example/"},
            {"model": "qwen-test-1", "choices": [],
             "usage": {}, "Location": "https://evil.example/",
             "_http_status": 302},
            {"model": "qwen-test-1", "choices": [],
             "usage": {}, "_http_status": 302},
        ]
        for payload in redirect_cases:
            real = OpenRouterModel(
                self._live_config(), lambda ref: "s3cr3t",
                transport=lambda u, h, b, t, m, p=payload: dict(p),
            )
            with self.assertRaisesRegex(ValueError, "REDIRECT_REJECTED"):
                real.decide({"kind": "observer", "org": "acme"}, {"content": "x"}, [])

        # Oversized injected response trips the max_bytes cap.
        big = OpenRouterModel(
            self._live_config(), lambda ref: "s3cr3t",
            transport=lambda u, h, b, t, m: {
                "model": "qwen-test-1",
                "choices": [{"message": {"content": json.dumps(
                    {"action": "ignore", "reason_code": "noise"})}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "pad": "x" * 70000,
            },
        )
        with self.assertRaisesRegex(ValueError, "RESPONSE_TOO_LARGE"):
            big.decide({"kind": "observer", "org": "acme"}, {"content": "x"}, [])

        # The production transport itself rejects redirects (no sockets:
        # stub the opener with a fake 302 response).
        import urllib.request

        import dots_farm.adapters as _adapters

        class _FakeHeaders:
            def get(self, name):
                return ("https://evil.example/"
                        if name.lower() == "location" else None)

        class _FakeResponse:
            status = 302
            headers = _FakeHeaders()

            def read(self, limit=None):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeOpener:
            def open(self, request, timeout=None):
                return _FakeResponse()

        real_build_opener = urllib.request.build_opener
        urllib.request.build_opener = lambda *a, **k: _FakeOpener()
        try:
            with self.assertRaisesRegex(ValueError, "REDIRECT_REJECTED"):
                _adapters._default_transport(
                    "https://openrouter.ai/api/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"hello": "world"}, 5, 65536,
                )
        finally:
            urllib.request.build_opener = real_build_opener

        # Management secret must never authenticate inference DOTS.
        mgmt_model = OpenRouterModel(
            self._live_config(secret_reference="openrouter_management_key"),
            lambda ref: "mgmt-plaintext",
            transport=lambda *a: {},
        )
        with self.assertRaises(PermissionError):
            mgmt_model.decide({"kind": "observer", "org": "acme"}, {"content": "hi"}, [])

        # Error paths never echo secrets or prompts.
        def failing(url, headers, body, timeout, max_bytes):
            raise ConnectionError("TRANSPORT_FAILED_UNKNOWN")

        err_model = OpenRouterModel(
            self._live_config(), lambda ref: "TOPSECRET", transport=failing
        )
        try:
            err_model.decide(
                {"kind": "observer", "org": "acme"}, {"content": "PROMPT-SENSITIVE"}, []
            )
            self.fail("expected error")
        except ConnectionError as exc:
            self.assertNotIn("TOPSECRET", str(exc))
            self.assertNotIn("PROMPT-SENSITIVE", str(exc))

    def test_key_cap_body_and_no_plaintext(self):
        seen = {}

        def transport(url, headers, body, timeout, max_bytes):
            seen.update(body)
            self.assertEqual(url, "https://openrouter.ai/api/v1/keys")
            return {"key": "sk-live-abc"}

        stored = {}

        def sink(dot_id, plaintext):
            stored["v"] = plaintext
            return "opaque-ref-%s" % dot_id

        mgr = OpenRouterKeyManager(lambda ref: "mgmt-key", sink,
                                   transport=transport)
        with self.assertRaises(PermissionError):
            mgr.provision("dot1", 2000000, approved=False)
        self.assertEqual(seen, {})
        out = mgr.provision("dot1", 2000000, approved=True)
        self.assertEqual(seen["limit"], 2.0)
        self.assertEqual(seen["limit_reset"], "monthly")
        self.assertTrue(seen["include_byok_in_limit"])
        self.assertNotIn("sk-live-abc", json.dumps(out))
        self.assertTrue(out["reference"].startswith("opaque-ref-"))
        self.assertEqual(stored["v"], "sk-live-abc")


class HermesPlanTests(unittest.TestCase):
    def test_clean_profile_plan_and_pinning(self):
        plan = profile_plan(
            {"id": "dot1", "kind": "observer", "model": "synthetic",
             "interval_seconds": 900},
            fixture_root(),
        )
        self.assertTrue(plan["candidate"])
        self.assertFalse(plan["cloned_from_existing"])
        self.assertFalse(plan["credentials_loaded"])
        self.assertEqual(plan["existing_profiles_touched"], [])
        self.assertTrue(plan["wake"]["script_only"] and plan["wake"]["no_agent"])
        self.assertTrue(plan["model_pin"]["no_global_fallback"])
        self.assertEqual(plan["writers"], ["dot1"])
        self.assertEqual(plan["memories"], {"fresh": True, "imported": []})
        self.assertFalse(plan["supervisor_powers"]["grant_tools"])
        self.assertFalse(plan["supervisor_powers"]["change_budgets"])
        self.assertFalse(plan["supervisor_powers"]["bypass_stop"])

    def test_supervisor_plan_budget_and_writer(self):
        plan = supervisor_plan(fixture_root(), 1000000)
        self.assertEqual(plan["dot_id"], "supervisor")
        self.assertEqual(plan["writers"], ["supervisor"])
        self.assertEqual(plan["budget_micro_usd"], 1000000)


_TEST_ONLY_SECRET_SENTINEL = "TEST_ONLY_SENTINEL_NOT_A_REAL_CREDENTIAL"
_TEST_ONLY_KEY_SENTINEL = "TEST_ONLY_KEY_MATERIAL_NOT_A_REAL_CREDENTIAL"
_MODEL_ID = "test-provider/test-model"
_PROVIDER_ID = "test-provider"
_MAX_MEMORY_CHARS = 2048


def _new_fixture_path(stem, suffix):
    """Allocate a retained fixture beneath DOTS_TEST_ROOT in the task workspace."""
    return Path(fixture_root()) / (stem + suffix)


def _valid_response(*, usage=None):
    return {
        "model": _MODEL_ID,
        "provider": _PROVIDER_ID,
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"action": "ignore", "reason_code": "noise"}
                    )
                }
            }
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.000001},
    }


def _model_config(**overrides):
    config = {
        "live_enabled": True,
        "approved_budget_micro_usd": 2_000_000,
        "model": _MODEL_ID,
        "qualified_models": [_MODEL_ID],
        "qualified_provider": _PROVIDER_ID,
        "provider_order": [_PROVIDER_ID],
        "secret_reference": "test-only-secret-reference",
        "max_tokens": 512,
        "tariff_per_token_micro": {"input": 100, "output": 100},
    }
    config.update(overrides)
    return config


def _make_model(*, response=None, **config_overrides):
    calls = []

    def injected_transport(url, headers, body, timeout, max_bytes):
        # Capture only request fields needed by assertions; never retain headers.
        calls.append(
            {
                "url": url,
                "body": copy.deepcopy(body),
                "timeout": timeout,
                "max_bytes": max_bytes,
            }
        )
        return copy.deepcopy(response or _valid_response())

    model = adapters.OpenRouterModel(
        _model_config(**config_overrides),
        secret_resolver=lambda _reference: _TEST_ONLY_SECRET_SENTINEL,
        transport=injected_transport,
    )
    return model, calls


def _wire_text(body):
    return json.dumps(body.get("messages", []), ensure_ascii=False, sort_keys=True)


class AdapterRegressionTests(unittest.TestCase):
    def test_live_request_pins_provider_privacy_and_bounded_tokens(self):
        model, calls = _make_model(max_tokens=1024)

        model.decide(
            {"kind": "observer", "org": "org-a"},
            {"id": "item-1", "org": "org-a", "content": "synthetic invoice summary"},
            memory="short synthetic memory",
        )

        self.assertEqual(len(calls), 1)
        body = calls[0]["body"]
        provider = body["provider"]
        self.assertEqual(provider["data_collection"], "deny")
        self.assertIs(provider["require_parameters"], True)
        self.assertNotIn("zdr", provider)
        self.assertIs(provider["allow_fallbacks"], False)
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertIs(body["response_format"]["json_schema"]["strict"], True)
        self.assertIsInstance(body["max_tokens"], int)
        self.assertNotIsInstance(body["max_tokens"], bool)
        self.assertGreaterEqual(body["max_tokens"], 1)
        self.assertLessEqual(body["max_tokens"], 1024)

    def test_invalid_max_tokens_are_rejected_before_transport(self):
        for max_tokens in (0, -1, 1025, 512.5, True):
            with self.subTest(max_tokens=repr(max_tokens)):
                model, calls = _make_model(max_tokens=max_tokens)
                with self.assertRaises(ValueError):
                    model.decide(
                        {"kind": "observer", "org": "org-a"},
                        {"id": "item-1", "org": "org-a", "content": "synthetic"},
                        memory="",
                    )
                self.assertEqual(calls, [])

    def test_prompt_has_trusted_typed_instruction_and_bounded_context_without_labels(self):
        model, calls = _make_model()
        large_memory = "MEMORY-PREFIX-ONLY:" + ("Ω" * 5000)
        gold_sentinel = "GOLD_LABEL_SENTINEL_MUST_NOT_BE_SENT"
        decision_sentinel = "FIXTURE_DECISION_SENTINEL_MUST_NOT_BE_SENT"

        model.decide(
            {"kind": "observer", "org": "org-a"},
            {
                "id": "item-1",
                "org": "org-a",
                "content": "SYNTHETIC-CONTENT-ONLY",
                "gold": gold_sentinel,
                "model_decision": {
                    "action": "alert",
                    "reason_code": decision_sentinel,
                },
            },
            memory=large_memory,
        )

        self.assertEqual(len(calls), 1)
        messages = calls[0]["body"]["messages"]
        trusted = "\n".join(
            message["content"]
            for message in messages
            if message.get("role") in {"system", "developer"}
            and isinstance(message.get("content"), str)
        )
        self.assertTrue(trusted, "A trusted system/developer decision instruction is required")
        trusted_lower = trusted.lower()
        self.assertIn("action", trusted_lower)
        self.assertIn("reason_code", trusted_lower)
        self.assertIn("type", trusted_lower)
        self.assertIn("string", trusted_lower)
        for code in ("ignore", "alert", "needs_owner"):
            self.assertIn(code, trusted_lower)

        wire = _wire_text(calls[0]["body"])
        context_text = "\n".join(
            message["content"]
            for message in messages
            if message.get("role") not in {"system", "developer"}
            and isinstance(message.get("content"), str)
        )
        self.assertIn("org-a", context_text)
        self.assertIn("observer", context_text)
        self.assertIn("SYNTHETIC-CONTENT-ONLY", context_text)
        self.assertIn("MEMORY-PREFIX-ONLY", context_text)
        self.assertNotIn("gold", context_text.lower())
        self.assertNotIn("model_decision", context_text.lower())
        self.assertNotIn("gold", wire.lower())
        self.assertNotIn("model_decision", wire.lower())
        memory_chars = context_text.count("Ω")
        self.assertGreater(memory_chars, 0)
        self.assertLessEqual(memory_chars, _MAX_MEMORY_CHARS)
        self.assertNotIn(gold_sentinel, wire)
        self.assertNotIn(decision_sentinel, wire)

    def test_malformed_usage_or_tariff_never_confirms_a_cost(self):
        cases = (
            (
                "negative-token-count",
                {"prompt_tokens": -1, "completion_tokens": 2, "cost": 0.000001},
                {"input": 10, "output": 100},
            ),
            (
                "float-token-count",
                {"prompt_tokens": 1.5, "completion_tokens": 2, "cost": 0.000001},
                {"input": 100, "output": 100},
            ),
            (
                "bool-token-count",
                {"prompt_tokens": True, "completion_tokens": 2, "cost": 0.000001},
                {"input": 100, "output": 100},
            ),
            (
                "negative-tariff-rate",
                {"prompt_tokens": 1, "completion_tokens": 2, "cost": 0.000001},
                {"input": -10, "output": 100},
            ),
            (
                "bool-tariff-rate",
                {"prompt_tokens": 1, "completion_tokens": 2, "cost": 0.000001},
                {"input": True, "output": 100},
            ),
            (
                "nonfinite-tariff-rate",
                {"prompt_tokens": 1, "completion_tokens": 2, "cost": 0.000001},
                {"input": float("inf"), "output": 100},
            ),
        )
        for case_id, usage, tariff in cases:
            with self.subTest(case=case_id):
                model, _calls = _make_model(
                    response=_valid_response(usage=usage),
                    tariff_per_token_micro=tariff,
                )
                try:
                    result = model.decide(
                        {"kind": "observer", "org": "org-a"},
                        {"id": "item-1", "org": "org-a", "content": "synthetic"},
                        memory="",
                    )
                except (TypeError, ValueError, OverflowError):
                    # A clear rejection is safe; a numeric confirmed cost is not.
                    continue
                self.assertIsNone(result["cost_micro_usd"])

    def test_fractional_decimal_string_tariff_is_valid(self):
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "cost": 0.000001}
        model, calls = _make_model(
            response=_valid_response(usage=usage),
            tariff_per_token_micro={"input": "0.075", "output": "0.125"},
        )

        result = model.decide(
            {"kind": "observer", "org": "org-a"},
            {"id": "item-1", "org": "org-a", "content": "synthetic"},
            memory="",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["cost_micro_usd"], 1)

    def test_fractional_numeric_tariff_is_not_rejected_as_malformed(self):
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "cost": 0.0002}
        model, calls = _make_model(
            response=_valid_response(usage=usage),
            tariff_per_token_micro={"input": 10.5, "output": 100},
        )

        result = model.decide(
            {"kind": "observer", "org": "org-a"},
            {"id": "item-1", "org": "org-a", "content": "synthetic"},
            memory="",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["cost_micro_usd"], 200)

    def _make_key_manager(self):
        calls = []

        def injected_transport(url, headers, body, timeout, max_bytes):
            # Never retain authorization headers or contact OpenRouter.
            calls.append({"url": url, "body": copy.deepcopy(body)})
            return {"key": _TEST_ONLY_KEY_SENTINEL}

        manager = adapters.OpenRouterKeyManager(
            secret_resolver=lambda _reference: _TEST_ONLY_SECRET_SENTINEL,
            secret_sink=lambda dot_id, _plaintext: f"test-only-reference:{dot_id}",
            transport=injected_transport,
        )
        return manager, calls

    def test_key_management_sends_a_numeric_usd_limit(self):
        manager, calls = self._make_key_manager()

        manager.provision("dots-a", 2_000_000, approved=True)

        self.assertEqual(len(calls), 1)
        limit = calls[0]["body"]["limit"]
        self.assertIsInstance(limit, (int, float))
        self.assertNotIsInstance(limit, bool)
        self.assertEqual(limit, 2)

    def test_key_management_uses_iso8601_utc_expiration_string(self):
        manager, calls = self._make_key_manager()
        expires_at = "2027-12-31T23:59:59Z"

        manager.provision("dots-a", 2_000_000, approved=True, expires_at=expires_at)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["body"]["expires_at"], expires_at)
        self.assertIsInstance(calls[0]["body"]["expires_at"], str)

    @staticmethod
    def _office_message(reason_code):
        return {
            "event_id": "item-1",
            "dot_id": "dots-a",
            "org": "org-a",
            "project": "synthetic-pilot",
            "kind": "alert",
            "reason_code": reason_code,
            "opaque_ref": "synthetic-ref-1",
            "hops": 0,
            "expires_at": 1_800_000_000,
        }

    def test_neutral_reason_noise_is_allowed_at_office_boundary(self):
        office = adapters.SyntheticOffice(_new_fixture_path("office-valid", ".sqlite"))

        result = office.send(self._office_message("noise"))

        self.assertEqual(result["reason_code"], "noise")
        self.assertEqual(office.count(), 1)

    def test_arbitrary_secret_as_reason_is_rejected_before_office_persistence(self):
        office = adapters.SyntheticOffice(_new_fixture_path("office-reject", ".sqlite"))

        with self.assertRaises(ValueError):
            office.send(self._office_message("secret_as_reason"))

        self.assertEqual(office.count(), 0)

    @staticmethod
    def _write_source(value):
        path = _new_fixture_path("source", ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        return adapters.SyntheticSource(path)

    @staticmethod
    def _poll_config(**overrides):
        config = {"org": "org-a", "max_queue": 10, "max_payload_bytes": 64}
        config.update(overrides)
        return config

    def _assert_source_explicitly_failed(self, source):
        with self.assertRaises((adapters._SourceFailed, adapters._SourceInconclusive)) as error:
            source.poll(self._poll_config())
        self.assertIn(str(error.exception), {"SOURCE_FAILED", "SOURCE_INCONCLUSIVE"})

    def test_oversize_synthetic_record_is_not_reported_as_a_valid_empty_poll(self):
        source = self._write_source(
            [{"id": "item-1", "org": "org-a", "content": "x" * 65}]
        )

        self._assert_source_explicitly_failed(source)

    def test_invalid_synthetic_content_is_not_silently_skipped(self):
        invalid_records = (
            {"id": "item-1", "org": "org-a", "content": None},
            {"id": "item-2", "org": "org-a", "content": 7},
        )
        for record in invalid_records:
            with self.subTest(record_id=record["id"]):
                source = self._write_source([record])
                self._assert_source_explicitly_failed(source)

    def test_a_genuinely_empty_synthetic_fixture_remains_a_valid_empty_poll(self):
        source = self._write_source([])

        self.assertEqual(source.poll(self._poll_config()), [])



class AdditionalAdapterRegressionTests(unittest.TestCase):
    def test_invoice_amount_and_fields_fail_with_typed_errors(self):
        invoice = {"org": "org-a", "number": "inv-1", "date": "2026-10-08",
                   "amount": "10.25", "currency": "USD"}
        missing = dict(invoice)
        missing.pop("amount")
        cases = [missing, dict(invoice, extra="private"),
                 dict(invoice, org="unsafe org"), dict(invoice, org="org-a\n")]
        cases += [dict(invoice, amount=value) for value in
                  (None, True, [], {}, "bad", "NaN", "Infinity", "0", "-1")]
        for malformed in cases:
            with self.subTest(invoice_fields=sorted(malformed)):
                with self.assertRaises(ValueError) as error:
                    validate_decision({"action": "collect", "reason_code": "invoice",
                                       "invoice": malformed}, "collector")
                self.assertTrue(str(error.exception).startswith("decision-bad-invoice"))
        result = validate_decision({"action": "collect", "reason_code": "invoice",
                                    "invoice": invoice}, "collector")
        self.assertEqual(result["invoice"]["amount"], "10.25")
        response = _valid_response()
        response["choices"][0]["message"]["content"] = json.dumps({
            "action": "collect", "reason_code": "invoice", "invoice": missing})
        model, calls = _make_model(response=response)
        with self.assertRaisesRegex(ValueError, "decision-bad-invoice-amount"):
            model.decide({"kind": "collector", "org": "org-a"}, {"content": "x"}, [])
        self.assertEqual(len(calls), 1)

    def test_invalid_pins_are_rejected_before_secrets_or_transport(self):
        cases = [
            {"model": value, "qualified_models": [value]}
            for value in ("", None, " ", "bad model", "vendor/", "vendor//name",
                          "vendor/name\n", "a" * 257)
        ]
        cases += [{"qualified_models": value} for value in (None, _MODEL_ID, 1, {})]
        cases += [{"qualified_provider": value, "provider_order": [value]}
                  for value in ("", None, " ", "test-provider\n")]
        cases += [{"provider_order": value}
                  for value in ([], None, _PROVIDER_ID, [_PROVIDER_ID, "other"])]
        cases += [{"model": value, "qualified_models": [value]}
                  for value in ("vendor/muse-spark-1.2", "vendor/model-contributor")]
        for overrides in cases:
            with self.subTest(fields=sorted(overrides)):
                model, calls = _make_model(**overrides)
                secret_calls = []
                model.secret_resolver = lambda ref: secret_calls.append(ref)
                with self.assertRaises(PermissionError):
                    model.decide({"kind": "observer", "org": "org-a"},
                                 {"content": "x"}, [])
                self.assertEqual(secret_calls, [])
                self.assertEqual(calls, [])
        model, calls = _make_model(provider_order=[None])
        model.config.pop("qualified_provider")
        with self.assertRaises(PermissionError):
            model.decide({"kind": "observer", "org": "org-a"}, {"content": "x"}, [])
        self.assertEqual(calls, [])

    def test_token_bounds_precede_secret_resolution(self):
        for value in (None, "512", 0, -1, 1025, True, 1.5, float("nan"), float("inf")):
            with self.subTest(value=repr(value)):
                model, calls = _make_model(max_tokens=value)
                secret_calls = []
                model.secret_resolver = lambda ref: secret_calls.append(ref)
                with self.assertRaisesRegex(ValueError, "model-bad-max-tokens"):
                    model.decide({"kind": "observer", "org": "org-a"},
                                 {"content": "x"}, [])
                self.assertEqual(secret_calls, [])
                self.assertEqual(calls, [])
        for value in (1, 1024):
            model, calls = _make_model(max_tokens=value)
            model.decide({"kind": "observer", "org": "org-a"}, {"content": "x"}, [])
            self.assertEqual(calls[0]["body"]["max_tokens"], value)

    def test_context_is_bounded_org_bound_and_memory_remains_untrusted(self):
        model, calls = _make_model()
        model.decide({"kind": "collector", "org": "org-a"},
                     {"org": "org-a", "content": "x" * 6000, "gold": "omit",
                      "model_decision": {"action": "collect"}},
                     [{"text": "y" * 6000, "authority": "owner"}])
        body = calls[0]["body"]
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(set(context), {"org", "kind", "content", "memory"})
        self.assertEqual(context["kind"], "collector")
        self.assertLessEqual(len(context["content"]), 4000)
        self.assertEqual(context["memory"]["trust"], "untrusted")
        self.assertLessEqual(len(context["memory"]["text"]), 2048)
        self.assertIn("collect", body["messages"][0]["content"])
        with self.assertRaises(PermissionError):
            model.decide({"kind": "observer", "org": "org-a"},
                         {"org": "org-b", "content": "x"}, [])
        self.assertEqual(len(calls), 1)

    def test_usage_counters_are_exact_nonnegative_integers(self):
        for key in ("prompt_tokens", "completion_tokens"):
            for value in (True, 1.5, float("nan"), float("inf"), -1, None, "1"):
                with self.subTest(key=key, value=repr(value)):
                    usage = {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0}
                    usage[key] = value
                    model, calls = _make_model(response=_valid_response(usage=usage))
                    with self.assertRaisesRegex(ValueError, "RESPONSE_BAD_USAGE"):
                        model.decide({"kind": "observer", "org": "org-a"},
                                     {"content": "x"}, [])
                    self.assertEqual(len(calls), 1)
        for usage in ({}, {"prompt_tokens": 1}):
            model, _calls = _make_model(response=_valid_response(usage=usage))
            with self.assertRaisesRegex(ValueError, "RESPONSE_BAD_USAGE"):
                model.decide({"kind": "observer", "org": "org-a"}, {"content": "x"}, [])

    def test_response_choices_are_typed_and_live_supervisor_is_not_qualified(self):
        response = _valid_response()
        response["choices"] = {0: response["choices"][0]}
        model, calls = _make_model(response=response)
        with self.assertRaisesRegex(ValueError, "RESPONSE_BAD_SHAPE"):
            model.decide({"kind": "observer", "org": "org-a"}, {"content": "x"}, [])
        self.assertEqual(len(calls), 1)
        model, calls = _make_model()
        with self.assertRaisesRegex(ValueError, "model-bad-kind"):
            model.decide({"kind": "supervisor", "org": "org-a"},
                         {"content": "x"}, [])
        self.assertEqual(calls, [])

    def test_missing_invalid_tariff_and_cost_never_fabricate_confirmation(self):
        for tariff in (None, {}, [], {"input": 0}, {"input": True, "output": 0},
                       {"input": -1, "output": 1}, {"input": "NaN", "output": 1},
                       {"input": 0, "output": float("inf")}):
            with self.subTest(tariff=repr(tariff)):
                model, _calls = _make_model(
                    response=_valid_response(usage={"prompt_tokens": 1,
                                                   "completion_tokens": 1, "cost": 0}),
                    tariff_per_token_micro=tariff)
                result = model.decide({"kind": "observer", "org": "org-a"},
                                      {"content": "x"}, [])
                self.assertIsNone(result["cost_micro_usd"])
        for value in (None, False, [], {}, "invalid", "NaN", "Infinity", -1):
            with self.subTest(cost=repr(value)):
                model, _calls = _make_model(response=_valid_response(usage={
                    "prompt_tokens": 1, "completion_tokens": 1, "cost": value}))
                result = model.decide({"kind": "observer", "org": "org-a"},
                                      {"content": "x"}, [])
                self.assertIsNone(result["cost_micro_usd"])
        model, _calls = _make_model(response=_valid_response(usage={
            "prompt_tokens": 1, "completion_tokens": 1}))
        self.assertIsNone(model.decide({"kind": "observer", "org": "org-a"},
                                       {"content": "x"}, [])["cost_micro_usd"])
        model, _calls = _make_model(tariff_per_token_micro={
            "input": Decimal("0.075"), "output": Decimal("0.125")})
        self.assertEqual(model.decide({"kind": "observer", "org": "org-a"},
                                     {"content": "x"}, [])["cost_micro_usd"], 1)

    def test_error_paths_redact_injected_secrets_and_never_retry(self):
        for exception_type in (TimeoutError, ConnectionError, ValueError, RuntimeError):
            with self.subTest(exception=exception_type.__name__):
                attempts = []

                def failing(*args):
                    attempts.append(1)
                    raise exception_type(_TEST_ONLY_SECRET_SENTINEL + " PRIVATE-PROMPT")

                model = OpenRouterModel(_model_config(),
                                        lambda ref: _TEST_ONLY_SECRET_SENTINEL,
                                        transport=failing)
                with self.assertRaises((TimeoutError, ConnectionError, ValueError)) as error:
                    model.decide({"kind": "observer", "org": "org-a"},
                                 {"content": "PRIVATE-PROMPT"}, [])
                self.assertEqual(attempts, [1])
                self.assertNotIn(_TEST_ONLY_SECRET_SENTINEL, str(error.exception))
                self.assertNotIn("PRIVATE-PROMPT", str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)

    def test_cost_rounding_preserves_fraction_above_a_micro_boundary(self):
        model, calls = _make_model(response=_valid_response(usage={
            "prompt_tokens": 1, "completion_tokens": 1,
            "cost": "0.0000010000000000000000000000000000000000000000001"}))
        result = model.decide({"kind": "observer", "org": "org-a"}, {"content": "x"}, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["cost_micro_usd"], 2)

    def test_key_expiry_is_normalized_before_any_secret_lookup(self):
        cases = ((1_800_000_000.75, "2027-01-15T08:00:00Z"),
                 ("2027-12-31T23:59:59.123Z", "2027-12-31T23:59:59Z"))
        for expiry, expected in cases:
            calls = []
            manager = OpenRouterKeyManager(
                lambda ref: _TEST_ONLY_SECRET_SENTINEL,
                lambda dot, key: "opaque-reference",
                transport=lambda url, headers, body, timeout, max_bytes:
                (calls.append(copy.deepcopy(body)) or {"key": _TEST_ONLY_KEY_SENTINEL}))
            manager.provision("dots-a", 75000, approved=True, expires_at=expiry)
            self.assertEqual(calls[0]["expires_at"], expected)
            self.assertEqual(calls[0]["limit"], 0.075)
        for value in (True, float("nan"), float("inf"), "bad", "2027-02-30T00:00:00Z"):
            touched = []
            manager = OpenRouterKeyManager(lambda ref: touched.append(ref),
                                           lambda dot, key: touched.append(dot),
                                           transport=lambda *args: touched.append("http"))
            with self.assertRaises(ValueError):
                manager.provision("dots-a", 1, approved=True, expires_at=value)
            self.assertEqual(touched, [])

    def test_key_transport_redirection_and_uncertain_failure_never_retries(self):
        for response in ({"_http_status": 302, "key": _TEST_ONLY_KEY_SENTINEL},
                         {"_redirect": True, "key": _TEST_ONLY_KEY_SENTINEL}):
            calls, saved = [], []

            def redirect(*args):
                calls.append(1)
                return response

            manager = OpenRouterKeyManager(lambda ref: _TEST_ONLY_SECRET_SENTINEL,
                                           lambda dot, key: saved.append(dot), transport=redirect)
            with self.assertRaisesRegex(ValueError, "REDIRECT_REJECTED"):
                manager.provision("dots-a", 1, approved=True)
            self.assertEqual(calls, [1])
            self.assertEqual(saved, [])
        attempts = []

        def timeout(*args):
            attempts.append(1)
            raise TimeoutError(_TEST_ONLY_SECRET_SENTINEL)

        manager = OpenRouterKeyManager(lambda ref: _TEST_ONLY_SECRET_SENTINEL,
                                       lambda dot, key: "opaque-reference", transport=timeout)
        with self.assertRaisesRegex(TimeoutError, "^PROVISION_UNKNOWN$"):
            manager.provision("dots-a", 1, approved=True)
        self.assertEqual(attempts, [1])

    def test_source_malformed_truncated_and_oversize_polls_are_visible(self):
        config = {"org": "org-a", "max_queue": 1, "max_payload_bytes": 64}
        valid = {"id": "item-1", "org": "org-a", "content": "x"}
        cases = [[valid, dict(valid, id="item-2")], [None], [dict(valid, content="Ω" * 33)],
                 [dict(valid, content=None)], [dict(valid, org=None)],
                 [dict(valid, id="unsafe id")], [{"org": "org-a", "content": "x"}]]
        for records in cases:
            with self.subTest(record_count=len(records)):
                path = _new_fixture_path("source", ".json")
                path.write_text(json.dumps(records), encoding="utf-8")
                with self.assertRaises((adapters._SourceFailed, adapters._SourceInconclusive)):
                    SyntheticSource(path).poll(config)
                self.assertTrue(path.exists())
        path = _new_fixture_path("source-truncated", ".json")
        path.write_text('[{"id":', encoding="utf-8")
        with self.assertRaisesRegex(adapters._SourceFailed, "^SOURCE_FAILED$"):
            SyntheticSource(path).poll(config)
        path.write_text(json.dumps([dict(valid, org="org-b")]), encoding="utf-8")
        self.assertEqual(SyntheticSource(path).poll(config), [])
        for bound in (True, 0, -1, 1.5):
            with self.assertRaises(ValueError):
                SyntheticSource(path).poll(dict(config, max_queue=bound))

    def test_office_reasons_are_closed_and_typed(self):
        office = SyntheticOffice(_new_fixture_path("office", ".sqlite"))
        for reason in ("secret_as_reason", "private-ref", None, True, [], {}):
            with self.assertRaisesRegex(ValueError, "office-bad-reason"):
                office.send(AdapterRegressionTests._office_message(reason))
        self.assertEqual(office.count(), 0)

    def test_target_concurrent_duplicate_and_restart_preserve_one_invoice(self):
        path = _new_fixture_path("target", ".sqlite")
        target = SyntheticTarget(path)
        payload = {"invoice": {"org": "org-a", "number": "inv-1", "date": "2026-10-08",
                                "amount": "10", "currency": "USD"}}
        with ThreadPoolExecutor(max_workers=4) as workers:
            receipts = list(workers.map(lambda index: target.put("effect-%s" % index, payload),
                                        range(4)))
        self.assertEqual(len({value["receipt"] for value in receipts}), 1)
        self.assertEqual(SyntheticTarget(path).count(), 1)
        uncertain = dict(payload, invoice=dict(payload["invoice"], number="inv-2"),
                         simulate_timeout_after_write=True)
        with self.assertRaises(_TargetTimeoutAfterWrite):
            target.put("uncertain", uncertain)
        reopened = SyntheticTarget(path)
        self.assertIsNotNone(reopened.lookup("uncertain"))
        self.assertEqual(reopened.count(), 2)

    def test_hermes_profiles_keep_independent_references_and_existing_files(self):
        base = Path(fixture_root())
        existing = base / "robert-marker.txt"
        existing.write_text("existing-profile-marker", encoding="utf-8")
        first = profile_plan({"id": "dots-a", "model": _MODEL_ID,
                              "secret_reference": "dot-a-reference"}, str(base))
        second = profile_plan({"id": "dots-b", "model": _MODEL_ID}, str(base))
        self.assertEqual(first["model_pin"]["model"], _MODEL_ID)
        self.assertEqual(first["secret_reference"], "dot-a-reference")
        for key in ("hermes_home", "memory_reference", "secret_reference"):
            self.assertNotEqual(first[key], second[key])
        self.assertEqual(first["writers"], ["dots-a"])
        self.assertEqual(first["live_qualification"], "BLOCKED")
        self.assertEqual(existing.read_text(encoding="utf-8"), "existing-profile-marker")
        self.assertFalse(Path(first["hermes_home"]).exists())
        self.assertEqual(list(base.iterdir()), [existing])
        for interval in (True, 0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                profile_plan({"id": "dots-a", "interval_seconds": interval}, str(base))
        with self.assertRaises(ValueError):
            profile_plan({"id": "dots-a", "secret_reference": "openrouter_management_key"},
                         str(base))


if __name__ == "__main__":
    unittest.main()
