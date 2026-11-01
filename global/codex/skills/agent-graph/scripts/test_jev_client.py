import io
import json
import math
import unittest
import urllib.error

from jev_client import (
    ENDPOINT,
    INPUT_USD_PER_TOKEN,
    JevError,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MODEL,
    PROVIDER,
    build_request,
    parse_response,
    request_decision,
    validate_normalized_decision,
)

SECRET = "synthetic-key-not-a-real-credential"


def make_task(**overrides):
    task = {
        "schema_version": 1,
        "brief": "Implement a small bounded helper and its tests.",
        "mode": "implement",
        "required_tools": ["shell"],
        "scope": ["global/codex/skills/agent-graph/scripts/"],
        "resources": [],
        "depends_on": [],
        "estimated_context_tokens": 12000,
        "domain": "routing",
        "high_risk": False,
    }
    task.update(overrides)
    return task


def make_candidate(cid="cand-a", **overrides):
    candidate = {
        "id": cid,
        "agent": "synthetic-cli",
        "model": "vendor/model-1",
        "lineage": "vendor-a",
        "enabled": True,
        "training_allowed": False,
        "billing": "subscription",
        "tools": ["shell"],
        "modes": ["implement"],
        "fit": {"routing": 0.8},
        "status": "available",
        "quota_remaining": 0.5,
        "quota_observed_at": "2026-09-25T20:00:00Z",
        "quota_reset_at": None,
        "context_window_tokens": 32000,
        "capacity_remaining": 1,
    }
    candidate.update(overrides)
    return candidate


def make_snapshot(candidates=None, **overrides):
    if candidates is None:
        candidates = [make_candidate()]
    snapshot = {
        "captured_at": "2026-09-25T21:00:00Z",
        "candidates": candidates,
        "active_assignments": [],
        "capacity": 2,
    }
    snapshot.update(overrides)
    return snapshot


def make_answers(candidate_ids=("cand-a",)):
    def choice(value, options):
        probabilities = {option: 0.0 for option in options}
        probabilities[value] = 0.9
        return {
            "type": "choice",
            "choice": value,
            "confidence": 0.9,
            "probabilities": probabilities,
        }

    answers = {
        "difficulty": choice("medium", ("low", "medium", "high")),
        "parallelism": choice("sequential", ("sequential", "parallel", "unknown")),
        "consultation": choice("none", ("none", "subscription", "api_candidate")),
    }
    for cid in candidate_ids:
        answers["fit_%s" % cid] = {
            "type": "score",
            "score": 3.5,
            "confidence": 0.9,
            "probabilities": {"0": 0, "1": 0, "2": 0, "3": 0.5, "4": 0.5},
            "legend": {"3": "adequate", "4": "strong"},
        }
    return answers


def make_response(candidate_ids=("cand-a",), **overrides):
    response = {
        "id": "gen-synthetic",
        "model": "typesafe/jev-1.13-20260917",
        "provider": PROVIDER,
        "answers": make_answers(candidate_ids),
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }
    response.update(overrides)
    return response


class FakeResponse:
    def __init__(self, payload, url=ENDPOINT):
        self._raw = payload if isinstance(payload, bytes) \
            else json.dumps(payload).encode("utf-8")
        self.url = url

    def read(self, limit=-1):
        return self._raw if limit < 0 else self._raw[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, behavior):
        self.behavior = behavior
        self.requests = []

    def open(self, request, timeout=15):
        self.requests.append(request)
        behavior = self.behavior
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def assert_code(test_case, code, func, *args, **kwargs):
    with test_case.assertRaises(JevError) as ctx:
        func(*args, **kwargs)
    test_case.assertEqual(ctx.exception.code, code)
    return ctx.exception


class BuildRequestTests(unittest.TestCase):
    def test_valid_body_shape_and_privacy_block(self):
        body = build_request(make_task(), make_snapshot())
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["provider"], {
            "data_collection": "deny",
            "allow_fallbacks": False,
            "only": [PROVIDER],
        })
        state = body["state"]
        self.assertEqual(set(state), {
            "task", "candidates", "captured_at", "active_assignments", "capacity"})
        questions = body["questions"]
        self.assertEqual(set(questions),
                         {"difficulty", "parallelism", "consultation", "fit_cand-a"})
        self.assertEqual(questions["difficulty"]["type"], "choice")
        self.assertEqual(set(questions["difficulty"]["criteria"]),
                         {"low", "medium", "high"})
        self.assertEqual(questions["fit_cand-a"]["type"], "score")
        self.assertEqual(questions["fit_cand-a"]["criteria"],
                         ["unsuitable", "weak", "adequate", "strong", "excellent"])
        serialized = json.dumps(body, ensure_ascii=True).encode("utf-8")
        self.assertLessEqual(len(serialized), MAX_REQUEST_BYTES)

    def test_question_ids_are_stable(self):
        first = build_request(make_task(), make_snapshot())["questions"]
        second = build_request(make_task(), make_snapshot())["questions"]
        self.assertEqual(list(first), list(second))

    def test_each_question_uses_instructions_field(self):
        # Offline contract assertion: the official Decisions API expects
        # `instructions` (not `question`) as the text field inside EACH
        # question (docs/JEV-ROUTING-DESIGN.md, integration note 2026-09-25;
        # tutorial lines 190-214; parent's live request without it got
        # HTTP400).
        questions = build_request(make_task(), make_snapshot())["questions"]
        self.assertTrue(questions)
        for question_id, question in questions.items():
            self.assertIn("instructions", question,
                          "question %r lacks instructions" % question_id)
            self.assertIsInstance(question["instructions"], str)
            self.assertTrue(question["instructions"])
            self.assertNotIn("question", question,
                             "question %r uses legacy question field"
                             % question_id)

    def test_rejects_bad_inputs(self):
        assert_code(self, "invalid_input", build_request,
                    make_task(schema_version=2), make_snapshot())
        assert_code(self, "invalid_input", build_request,
                    make_task(mode="delete"), make_snapshot())
        assert_code(self, "invalid_input", build_request,
                    make_task(brief="x" * 4001), make_snapshot())
        assert_code(self, "invalid_input", build_request,
                    make_task(estimated_context_tokens=1.5), make_snapshot())
        assert_code(self, "invalid_input", build_request,
                    make_task(high_risk="yes"), make_snapshot())
        assert_code(self, "invalid_input", build_request,
                    make_task(), make_snapshot(capacity=-1))
        assert_code(self, "invalid_input", build_request,
                    make_task(), make_snapshot(captured_at="not-a-date"))

    def test_rejects_bad_candidates(self):
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(notes="raw text")]))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(id="bad id/with spaces")]))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(), make_candidate()]))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(quota_remaining=1.5)]))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(fit={"routing": math.nan})]))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(status="busy")]))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(billing="barter")]))

    def test_rejects_privacy_violating_inputs(self):
        # Identities are fictional and paths are never opened.
        assert_code(self, "privacy_refused", build_request,
                    make_task(brief="open C:\\Users\\synthetic-fixture-owner\\secrets.txt"),
                    make_snapshot())
        assert_code(self, "privacy_refused", build_request,
                    make_task(brief="key is sk-abc123def"), make_snapshot())
        assert_code(self, "privacy_refused", build_request, make_task(),
                    make_snapshot([make_candidate(agent="Bearer token-here")]))

    def test_token_marker_does_not_match_inside_ordinary_words(self):
        build_request(make_task(brief="Keep this task-bound and desk-based."), make_snapshot())
        assert_code(self, "privacy_refused", build_request,
                    make_task(brief="Token sk-abc123def must be redacted."), make_snapshot())

    def test_optional_native_metadata_is_validated_without_aliases(self):
        build_request(make_task(), make_snapshot([make_candidate(model_class="unknown", coding_rank=None)]))
        build_request(make_task(), make_snapshot([make_candidate(model_class="opus", coding_rank=2)]))
        for changes in ({"model_class": "opus-ultra"}, {"model_class": []},
                        {"coding_rank": True}, {"coding_rank": float("inf")},
                        {"coding_rank": 0}, {"modes": ["invented"]}):
            assert_code(self, "invalid_input", build_request, make_task(),
                        make_snapshot([make_candidate(**changes)]))

    def test_requires_utc_aware_iso_timestamps(self):
        # Naive and non-UTC offsets are rejected: captured_at must be UTC ISO.
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot(captured_at="2026-09-25T21:00:00"))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot(captured_at="2026-09-25T23:00:00+02:00"))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(
                        quota_observed_at="2026-09-25T20:00:00")]))
        assert_code(self, "invalid_input", build_request, make_task(),
                    make_snapshot([make_candidate(
                        quota_reset_at="2026-09-26T02:00:00+02:00")]))
        # Explicit UTC forms pass.
        build_request(make_task(), make_snapshot(
            captured_at="2026-09-25T21:00:00+00:00",
            candidates=[make_candidate(
                quota_observed_at="2026-09-25T20:00:00+00:00",
                quota_reset_at="2026-09-26T00:00:00Z")]))


class ParseResponseTests(unittest.TestCase):
    def setUp(self):
        self.request = build_request(make_task(), make_snapshot())

    def test_normalizes_happy_path(self):
        decision = parse_response(make_response(), self.request)
        self.assertEqual(decision["model"], "typesafe/jev-1.13-20260917")
        self.assertEqual(decision["provider"], PROVIDER)
        self.assertEqual(decision["decision_id"], "gen-synthetic")
        self.assertEqual(decision["difficulty"],
                         {"value": "medium", "confidence": 0.9})
        self.assertEqual(decision["parallelism"]["value"], "sequential")
        self.assertEqual(decision["consultation"]["value"], "none")
        self.assertEqual(decision["fit"]["cand-a"],
                         {"score": 3.5, "confidence": 0.9})
        self.assertEqual(decision["usage"], {
            "input_tokens": 476,
            "output_tokens": 70,
            "cost_usd": 0.000019992,
        })
        self.assertEqual(decision["pricing_basis"]["input_usd_per_token"],
                         INPUT_USD_PER_TOKEN)
        self.assertEqual(decision["pricing_basis"]["verified_at"], "2026-09-25")

    def test_accepts_exact_model_and_dated_suffix(self):
        decision = parse_response(make_response(model=MODEL), self.request)
        self.assertEqual(decision["model"], MODEL)
        assert_code(self, "model_mismatch", parse_response,
                    make_response(model="other/jev-9"), self.request)
        assert_code(self, "model_mismatch", parse_response,
                    make_response(model="typesafe/jev-1.13-beta"), self.request)

    def test_rejects_foreign_variant_with_dated_suffix(self):
        assert_code(self, "model_mismatch", parse_response,
                    make_response(model="typesafe/jev-1.13-turbo-20260917"),
                    self.request)
        assert_code(self, "model_mismatch", parse_response,
                    make_response(model="typesafe/jev-1.13-20260917-rc1"),
                    self.request)

    def test_provider_must_match_typesafe_exactly(self):
        for provider in ("Other", "typesafe", "TypeSafe ", "", None):
            assert_code(self, "provider_mismatch", parse_response,
                        make_response(provider=provider), self.request)

    def test_request_must_have_exact_typesafe_provider_filter(self):
        for provider in (None, {}, {"only": ["Other"]},
                         {"only": [PROVIDER, "Other"]},
                         {"only": PROVIDER},
                         {"data_collection": "allow", "allow_fallbacks": False,
                          "only": [PROVIDER]},
                         {"data_collection": "deny", "allow_fallbacks": True,
                          "only": [PROVIDER]}):
            request = {**self.request, "provider": provider}
            assert_code(self, "invalid_input", parse_response,
                        make_response(), request)

    def test_rejects_missing_or_extra_answers(self):
        response = make_response()
        del response["answers"]["difficulty"]
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)
        response = make_response()
        response["answers"]["extra"] = response["answers"]["difficulty"]
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)

    def test_rejects_bad_choice_and_distribution(self):
        response = make_response()
        response["answers"]["difficulty"]["choice"] = "extreme"
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)
        response = make_response()
        response["answers"]["difficulty"]["probabilities"] = {"low": 1.0}
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)
        response = make_response()
        response["answers"]["difficulty"]["probabilities"]["low"] = 1.5
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)
        response = make_response()
        response["answers"]["difficulty"]["confidence"] = float("nan")
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)

    def test_rejects_bad_score(self):
        response = make_response()
        response["answers"]["fit_cand-a"]["score"] = 4.5
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)
        response = make_response()
        response["answers"]["fit_cand-a"]["score"] = float("inf")
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)
        response = make_response()
        response["answers"]["fit_cand-a"]["probabilities"] = {"0": 1}
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)

    def test_usage_cost_unknown_stays_none_not_zero(self):
        response = make_response()
        del response["usage"]["cost"]
        decision = parse_response(response, self.request)
        self.assertIsNone(decision["usage"]["cost_usd"])
        response = make_response(usage={})
        decision = parse_response(response, self.request)
        self.assertEqual(decision["usage"], {
            "input_tokens": None, "output_tokens": None, "cost_usd": None})
        response = make_response()
        response["usage"]["cost"] = -0.1
        assert_code(self, "invalid_response", parse_response,
                    response, self.request)
        for usage in ([], {"input_tokens": True}, {"output_tokens": -1},
                      {"cost": float("nan")}, {"cost": float("inf")}):
            assert_code(self, "invalid_response", parse_response,
                        make_response(usage=usage), self.request)

    def test_stored_decision_shape_and_candidate_ids(self):
        decision = parse_response(make_response(), self.request)
        self.assertIs(validate_normalized_decision(decision, make_snapshot()), decision)
        for mutation in ({"provider": "Other"}, {"fit": {}},
                         {"usage": {"input_tokens": float("nan"), "output_tokens": 1, "cost_usd": None}}):
            edited = {**decision, **mutation}
            assert_code(self, "invalid_response", validate_normalized_decision,
                        edited, make_snapshot())


class RequestDecisionTests(unittest.TestCase):
    def test_sends_one_bounded_request(self):
        opener = FakeOpener(FakeResponse(make_response()))
        decision = request_decision(make_task(), make_snapshot(), SECRET,
                                    max_cost_usd=0.01, opener=opener)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers["Authorization"],
                         "Bearer %s" % SECRET)
        self.assertLessEqual(len(request.data), MAX_REQUEST_BYTES)
        self.assertEqual(decision["difficulty"]["value"], "medium")

    def test_cost_gate(self):
        opener = FakeOpener(FakeResponse(make_response()))
        assert_code(self, "budget_exceeded", request_decision,
                    make_task(), make_snapshot(), SECRET,
                    max_cost_usd=0.0000000001, opener=opener)
        for bad in (0, -1, float("nan"), float("inf"), "1.0"):
            assert_code(self, "budget_exceeded", request_decision,
                        make_task(), make_snapshot(), SECRET,
                        max_cost_usd=bad, opener=opener)
        self.assertEqual(opener.requests, [])

    def test_requires_explicit_key(self):
        assert_code(self, "invalid_input", request_decision,
                    make_task(), make_snapshot(), "", max_cost_usd=0.01,
                    opener=FakeOpener(FakeResponse(make_response())))
        assert_code(self, "invalid_input", request_decision,
                    make_task(), make_snapshot(), None, max_cost_usd=0.01,
                    opener=FakeOpener(FakeResponse(make_response())))

    def test_redirect_refused(self):
        opener = FakeOpener(FakeResponse(make_response(),
                                         url="https://evil.example/"))
        assert_code(self, "redirect_refused", request_decision,
                    make_task(), make_snapshot(), SECRET,
                    max_cost_usd=0.01, opener=opener)
        redirect = urllib.error.HTTPError(ENDPOINT, 302, "Found", {}, io.BytesIO())
        assert_code(self, "redirect_refused", request_decision,
                    make_task(), make_snapshot(), SECRET,
                    max_cost_usd=0.01, opener=FakeOpener(redirect))

    def test_http_and_transport_errors_have_sanitized_codes(self):
        http_error = urllib.error.HTTPError(
            ENDPOINT, 400, "privacy fields unsupported", {}, io.BytesIO())
        error = assert_code(self, "http_error_400", request_decision,
                            make_task(), make_snapshot(), SECRET,
                            max_cost_usd=0.01, opener=FakeOpener(http_error))
        self.assertNotIn("unsupported", str(error))
        transport = urllib.error.URLError("connection refused")
        assert_code(self, "transport_error", request_decision,
                    make_task(), make_snapshot(), SECRET,
                    max_cost_usd=0.01, opener=FakeOpener(transport))

    def test_timeout_and_response_read_errors_are_sanitized(self):
        class BrokenResponse(FakeResponse):
            def read(self, limit=-1):
                raise TimeoutError("secret provider response")
        for failure in (TimeoutError("secret provider response"),
                        RuntimeError("secret provider response")):
            error = assert_code(self, "transport_error", request_decision,
                                make_task(), make_snapshot(), SECRET,
                                max_cost_usd=0.01, opener=FakeOpener(failure))
            self.assertNotIn("secret", str(error))
        error = assert_code(self, "transport_error", request_decision,
                            make_task(), make_snapshot(), SECRET,
                            max_cost_usd=0.01, opener=FakeOpener(BrokenResponse(make_response())))
        self.assertNotIn("secret", str(error))

    def test_response_cap_and_invalid_json(self):
        huge = FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1))
        assert_code(self, "response_too_large", request_decision,
                    make_task(), make_snapshot(), SECRET,
                    max_cost_usd=1.0, opener=FakeOpener(huge))
        garbage = FakeResponse(b"not json")
        assert_code(self, "invalid_response", request_decision,
                    make_task(), make_snapshot(), SECRET,
                    max_cost_usd=1.0, opener=FakeOpener(garbage))

    def test_errors_never_leak_key_or_prompt(self):
        task = make_task(brief="UNIQUE-SYNTHETIC-PROMPT-MARKER")
        cases = [
            FakeOpener(urllib.error.HTTPError(ENDPOINT, 500, "x", {}, io.BytesIO())),
            FakeOpener(urllib.error.URLError("down")),
            FakeOpener(FakeResponse(b"{{")),
        ]
        for opener in cases:
            with self.assertRaises(JevError) as ctx:
                request_decision(task, make_snapshot(), SECRET,
                                 max_cost_usd=1.0, opener=opener)
            message = str(ctx.exception)
            self.assertNotIn(SECRET, message)
            self.assertNotIn("UNIQUE-SYNTHETIC-PROMPT-MARKER", message)


if __name__ == "__main__":
    unittest.main()
