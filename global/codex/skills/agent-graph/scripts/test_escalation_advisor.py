def _redacted_fixture(kind, ordinal, binary=False):
    """Return a deterministic public synthetic fixture value.

    The result is newly authored public data derived only from the pinned
    rule label and a per-file ordinal. It is never derived from any original
    value. Runtime values retain the original detector category.
    """
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
    seed = (len(kind) * 131 + int(ordinal) * 7919 + 17) & 0x7FFFFFFF
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        chars.append(alphabet[seed % 36])
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = 'gh' + 'p_' + body
    elif kind == 'aws-access-key-id':
        body = 'AK' + 'IA' + body[:16].upper()
    elif kind == 'private-key':
        marker = 'PRIVATE' + ' KEY'
        body = ('-----' + 'BEGIN ' + marker + '-----' + chr(10) + body + chr(10)
                + '-----' + 'END ' + marker + '-----')
    if binary:
        return body.encode('ascii')
    return body


import io
import json
import math
import tempfile
import unittest
import urllib.error
from pathlib import Path

from escalation_advisor import (
    AdvisorError,
    ROUTE_CHOICES,
    build_request,
    case_digest,
    load_case,
    minimize_case,
    parse_response,
    run_advise,
    run_baseline,
    write_new,
)
from escalation_policy import GUARDED_PROMOTION_THRESHOLD
from jev_client import ENDPOINT, MODEL, PROVIDER

SECRET = "synthetic-key-not-a-real-credential"


def make_case(**overrides):
    case = {
        "schema_version": 1,
        "id": "case-1",
        "summary": "Report totals regressed after the last release.",
        "category": "technical_bug",
        "baseline_reference": "release-2026-09-20",
        "business_change": False,
        "scope_change": False,
        "restores_baseline": True,
        "reproduced": True,
        "hard_gates": [],
        "approved_gates": [],
        "technical_uncertainty": False,
    }
    case.update(overrides)
    return case


def make_answer(choice="lead", confidence=0.9):
    probabilities = {option: 0.0 for option in ROUTE_CHOICES}
    selected_probability = max(0.25, confidence)
    probabilities = {option: (1.0-selected_probability)/4 for option in ROUTE_CHOICES}
    probabilities[choice] = selected_probability
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
    }




def make_response(**overrides):
    response = {
        "id": "gen-synthetic",
        "model": "typesafe/jev-1.13-20260926",
        "provider": PROVIDER,
        "answers": {"route": make_answer()},
        "usage": {"input_tokens": 210, "output_tokens": 12, "cost": 0.0000042},
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


def write_case(directory, case):
    path = Path(directory) / "case.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    return str(path)


class MinimizeCaseTests(unittest.TestCase):
    def test_explicit_redacted_summary_provides_meaningful_context(self):
        case = make_case(summary='A required field should become optional.')
        value = minimize_case(case, include_redacted_summary=True)
        self.assertEqual(value['problem_summary'], case['summary'])
        self.assertEqual(value['summary_trust'], 'untrusted-parent-minimized-data')
        self.assertNotIn('baseline_reference', value)

    def test_obvious_sensitive_summary_is_refused_before_network(self):
        for summary in ('mail a@example.org', 'https://customer.example/ticket/1',
                        'D:\\customer\\record', '```print(1)```', 'phone 123456789',
                        'password=super-secret-fixture', 'x' * 1201):
            with self.subTest(summary=summary), self.assertRaises(AdvisorError):
                build_request(make_case(summary=summary), include_redacted_summary=True)

    def test_missing_nullable_flags_preserve_unknown(self):
        case = make_case()
        del case['business_change']
        del case['scope_change']
        value = minimize_case(case)
        self.assertIsNone(value['business_change'])
        self.assertIsNone(value['scope_change'])

    def test_other_provider_is_rejected(self):
        with self.assertRaises(AdvisorError):
            parse_response(make_response(provider='another-provider'))

    def test_excludes_free_text_id_and_approved_gates(self):
        case = make_case(
            summary="Customer Jane Doe reported this at acme-corp.com",
            baseline_reference="D:\\AL Lab\\private\\ticket-9.md",
            hard_gates=["production"], approved_gates=["production"],
        )
        state = minimize_case(case)
        self.assertNotIn("summary", state)
        self.assertNotIn("id", state)
        self.assertNotIn("baseline_reference", state)
        self.assertNotIn("approved_gates", state)
        self.assertEqual(state["has_baseline_reference"], True)
        self.assertEqual(state["hard_gates"], ["production"])
        serialized = json.dumps(state)
        self.assertNotIn("Jane Doe", serialized)
        self.assertNotIn("acme-corp", serialized)
        self.assertNotIn("ticket-9", serialized)

    def test_whitespace_only_baseline_reference_is_not_declared(self):
        # categorize() treats a whitespace-only reference as missing
        # (BASELINE_REFERENCE_MISSING); the metadata sent to JEV must agree,
        # not claim a baseline exists that the policy already discards.
        for reference in (" ", "\t\r\n", "  "):
            with self.subTest(reference=repr(reference)):
                state = minimize_case(make_case(baseline_reference=reference))
                self.assertEqual(state["has_baseline_reference"], False)

    def test_rejects_sensitive_content_even_in_kept_fields(self):
        with self.assertRaises(AdvisorError) as ctx:
            minimize_case(make_case(category=("Bearer " + _redacted_fixture('bearer-token', 1))))
        self.assertEqual(ctx.exception.code, "privacy_refused")


class BuildRequestTests(unittest.TestCase):
    def test_single_route_question_with_fixed_privacy_block(self):
        body, payload = build_request(make_case())
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["provider"], {
            "data_collection": "deny",
            "allow_fallbacks": False,
            "only": [PROVIDER],
        })
        self.assertEqual(set(body["questions"]), {"route"})
        question = body["questions"]["route"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(set(question["criteria"]), set(ROUTE_CHOICES))
        self.assertEqual(set(body["state"]), {"case"})
        self.assertLessEqual(len(payload), 4000)

    def test_no_caller_defined_endpoint_in_request_shape(self):
        body, _ = build_request(make_case())
        self.assertNotIn("endpoint", body)
        self.assertNotIn("url", body)


class ParseResponseTests(unittest.TestCase):
    def test_normalizes_happy_path(self):
        decision = parse_response(make_response())
        self.assertEqual(decision["route"], "lead")
        self.assertAlmostEqual(decision["confidence"], 0.9)
        self.assertEqual(decision["usage"], {
            "input_tokens": 210, "output_tokens": 12, "cost_usd": 0.0000042,
        })

    def test_rejects_malformed_model_and_provider(self):
        with self.assertRaises(AdvisorError):
            parse_response(make_response(model="other/jev-9"))
        with self.assertRaises(AdvisorError):
            parse_response(make_response(provider=""))

    def test_rejects_unknown_route_choice(self):
        response = make_response()
        response["answers"]["route"]["choice"] = "downgrade-owner-to-self"
        with self.assertRaises(AdvisorError) as ctx:
            parse_response(response)
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_rejects_extra_or_missing_answer_keys(self):
        response = make_response()
        response["answers"]["extra"] = response["answers"]["route"]
        with self.assertRaises(AdvisorError):
            parse_response(response)
        response = make_response()
        del response["answers"]["route"]
        with self.assertRaises(AdvisorError):
            parse_response(response)

    def test_rejects_nan_confidence_and_bad_probabilities(self):
        response = make_response()
        response["answers"]["route"]["confidence"] = math.nan
        with self.assertRaises(AdvisorError):
            parse_response(response)
        response = make_response()
        response["answers"]["route"]["probabilities"] = {"lead": 1.0}
        with self.assertRaises(AdvisorError):
            parse_response(response)

    def test_rejects_choice_inconsistent_with_its_own_probability(self):
        # choice=architect but probabilities say architect=0, lead=1: an
        # internally inconsistent response must never reach the overlay.
        response = make_response()
        probabilities = {option: 0.0 for option in ROUTE_CHOICES}
        probabilities["lead"] = 1.0
        response["answers"]["route"] = {
            "type": "choice", "choice": "architect", "confidence": 0.99,
            "probabilities": probabilities,
        }
        with self.assertRaises(AdvisorError) as ctx:
            parse_response(response)
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_accepts_tied_maximum_probability_for_choice(self):
        response = make_response()
        probabilities = {option: 0.0 for option in ROUTE_CHOICES}
        probabilities["architect"] = 0.5
        probabilities["lead"] = 0.5
        response["answers"]["route"] = {
            "type": "choice", "choice": "architect", "confidence": 0.5,
            "probabilities": probabilities,
        }
        decision = parse_response(response)
        self.assertEqual(decision["route"], "architect")

    def test_absent_usage_stays_unknown_not_zero(self):
        response = make_response(usage={})
        decision = parse_response(response)
        self.assertEqual(decision["usage"], {
            "input_tokens": None, "output_tokens": None, "cost_usd": None})

    def test_self_reported_confidence_cannot_exceed_its_own_probability_mass(self):
        # choice=architect, confidence=0.99, but the choice's own probability
        # mass is only a tied 0.25: a model cannot cross the guarded
        # promotion threshold by inflating "confidence" independently of
        # "probabilities". The signal used downstream must be capped.
        response = make_response()
        probabilities = {option: 0.25 for option in ROUTE_CHOICES[:4]}
        probabilities[ROUTE_CHOICES[4]] = 0.0
        probabilities["architect"] = 0.25
        response["answers"]["route"] = {
            "type": "choice", "choice": "architect", "confidence": 0.99,
            "probabilities": probabilities,
        }
        decision = parse_response(response)
        self.assertEqual(decision["route"], "architect")
        self.assertAlmostEqual(decision["confidence"], 0.25)
        self.assertLess(decision["confidence"], GUARDED_PROMOTION_THRESHOLD)




    def test_invalid_probability_total_is_rejected(self):
        for values in ((.9,.9,0,0,0),(.2,.1,0,0,0)):
            with self.subTest(values=values):
                response=make_response()
                response["answers"]["route"]={"type":"choice", "choice":ROUTE_CHOICES[0],
                    "confidence":.9,"probabilities":dict(zip(ROUTE_CHOICES,values))}
                with self.assertRaises(AdvisorError) as ctx: parse_response(response)
                self.assertEqual(ctx.exception.code,"invalid_response")

    def test_rounded_distribution_and_distinct_confidence_are_valid(self):
        for values in ((.78,.06,.05,.05,.05),(.78,.06,.06,.06,.05)):
            response=make_response()
            response["answers"]["route"]={"type":"choice", "choice":ROUTE_CHOICES[0],
                "confidence":.67,"probabilities":dict(zip(ROUTE_CHOICES,values))}
            self.assertEqual(parse_response(response)["confidence"],.67)


class CaseDigestTests(unittest.TestCase):
    def test_digest_is_deterministic_and_binds_to_full_case_content(self):
        case_a = make_case()
        case_b = make_case()
        self.assertEqual(case_digest(case_a), case_digest(case_b))
        different = make_case(summary="A different summary entirely.")
        self.assertNotEqual(case_digest(case_a), case_digest(different))

    def test_digest_never_contains_raw_case_text(self):
        digest = case_digest(make_case(summary="UNIQUE-SYNTHETIC-MARKER"))
        self.assertEqual(set(digest), {"algorithm", "value"})
        self.assertEqual(digest["algorithm"], "sha256")
        self.assertNotIn("UNIQUE-SYNTHETIC-MARKER", json.dumps(digest))


class RunBaselineTests(unittest.TestCase):
    def test_offline_baseline_never_touches_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = make_case()
            case_path = write_case(tmp, case)
            report = run_baseline(case_path)
        self.assertEqual(report["mode"], "baseline")
        self.assertEqual(report["jev_consultation"]["status"], "unavailable")
        self.assertEqual(report["execution"], "NOT_DISPATCHED")
        self.assertEqual(report["result"]["decision_route"], "TEAM_FIX")
        self.assertFalse(report["result"]["authorization"])
        self.assertTrue(report["result"]["no_dispatch"])
        self.assertEqual(report["case_digest"], case_digest(case))

    def test_declared_owner_route_cannot_be_downgraded_by_self(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case(category="business_logic"))
            report = run_baseline(case_path)
        self.assertEqual(report["result"]["decision_route"], "OWNER_DECISION")

    def test_reproduced_regression_stays_team_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report = run_baseline(case_path)
        self.assertEqual(report["result"]["decision_route"], "TEAM_FIX")
        self.assertIn("REPRODUCED_BASELINE_REGRESSION", report["result"]["reasoning_codes"])


class RunAdviseTests(unittest.TestCase):
    def test_sends_exactly_one_request_and_records_shadow_signal(self):
        opener = FakeOpener(FakeResponse(make_response(answers={"route": make_answer("self", 0.95)})))
        with tempfile.TemporaryDirectory() as tmp:
            case = make_case()
            case_path = write_case(tmp, case)
            report = run_advise(case_path, 0.01, SECRET, opener=opener)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(report["jev_consultation"]["status"], "consulted")
        self.assertEqual(report["result"]["jev_shadow"]["route"], "self")
        # The deterministic route is never altered by the shadow signal, even
        # when Jev suggests downgrading an owner-worthy or already-decided route.
        self.assertEqual(report["result"]["decision_route"], "TEAM_FIX")
        self.assertEqual(report["case_digest"], case_digest(case))

    def test_declared_owner_route_survives_a_self_shadow_signal(self):
        opener = FakeOpener(FakeResponse(make_response(answers={"route": make_answer("self", 0.99)})))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case(category="business_logic"))
            report = run_advise(case_path, 0.01, SECRET, opener=opener)
        self.assertEqual(report["result"]["decision_route"], "OWNER_DECISION")
        self.assertFalse(report["result"]["authorization"])

    def test_budget_refusal_sends_nothing(self):
        opener = FakeOpener(FakeResponse(make_response()))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report = run_advise(case_path, 0.0, SECRET, opener=opener)
        self.assertEqual(opener.requests, [])
        self.assertEqual(report["jev_consultation"]["status"], "unavailable")
        self.assertEqual(report["jev_consultation"]["reason"], "budget_exceeded")
        # The local baseline stays usable even though Jev was never consulted.
        self.assertEqual(report["result"]["decision_route"], "TEAM_FIX")

    def test_missing_key_sends_nothing_and_preserves_baseline(self):
        opener = FakeOpener(FakeResponse(make_response()))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report = run_advise(case_path, 0.01, "", opener=opener)
        self.assertEqual(opener.requests, [])
        self.assertEqual(report["jev_consultation"]["reason"], "invalid_input")

    def test_transport_and_http_errors_are_sanitized_and_preserve_baseline(self):
        for opener in (
            FakeOpener(urllib.error.URLError("connection refused")),
            FakeOpener(urllib.error.HTTPError(ENDPOINT, 500, "server said something private", {}, io.BytesIO())),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                case_path = write_case(tmp, make_case())
                report = run_advise(case_path, 0.01, SECRET, opener=opener)
            self.assertEqual(report["jev_consultation"]["status"], "unavailable")
            self.assertNotIn("private", json.dumps(report))
            self.assertNotIn(SECRET, json.dumps(report))
            self.assertEqual(report["result"]["decision_route"], "TEAM_FIX")

    def test_redirect_is_refused_and_preserves_baseline(self):
        opener = FakeOpener(FakeResponse(make_response(), url="https://evil.example/"))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report = run_advise(case_path, 0.01, SECRET, opener=opener)
        self.assertEqual(report["jev_consultation"]["reason"], "redirect_refused")

    def test_malformed_response_never_leaks_case_content(self):
        opener = FakeOpener(FakeResponse(b"not json"))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case(summary="UNIQUE-SYNTHETIC-MARKER"))
            report = run_advise(case_path, 0.01, SECRET, opener=opener)
        self.assertEqual(report["jev_consultation"]["reason"], "invalid_response")
        self.assertNotIn("UNIQUE-SYNTHETIC-MARKER", json.dumps(report))

    def test_rejected_route_answer_still_preserves_real_recorded_usage(self):
        # The provider round trip actually happened and reported real usage,
        # but the route answer itself is internally inconsistent (choice's
        # probability is not the max) and must be rejected. That rejection
        # must not silently discard the already-known token/cost usage.
        probabilities = {option: 0.0 for option in ROUTE_CHOICES}
        probabilities["lead"] = 1.0
        response = make_response(answers={"route": {
            "type": "choice", "choice": "architect", "confidence": 0.99,
            "probabilities": probabilities,
        }}, usage={"input_tokens": 210, "output_tokens": 12, "cost": 0.0000042})
        opener = FakeOpener(FakeResponse(response))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report = run_advise(case_path, 0.01, SECRET, opener=opener)
        self.assertEqual(report["jev_consultation"]["status"], "unavailable")
        self.assertEqual(report["jev_consultation"]["reason"], "invalid_response")
        self.assertEqual(report["jev_consultation"]["usage"], {
            "input_tokens": 210, "output_tokens": 12, "cost_usd": 0.0000042})
        # Never fabricated when genuinely absent/malformed (network failures
        # never reached a parsed response at all).
        opener2 = FakeOpener(urllib.error.URLError("connection refused"))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report2 = run_advise(case_path, 0.01, SECRET, opener=opener2)
        self.assertNotIn("usage", report2["jev_consultation"])


class RunAdvisePolicyModeTests(unittest.TestCase):
    def test_default_policy_mode_is_shadow_and_backward_compatible(self):
        opener = FakeOpener(FakeResponse(make_response(answers={"route": make_answer("architect", 0.99)})))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report = run_advise(case_path, 0.01, SECRET, opener=opener)
        self.assertEqual(report["policy_mode"], "shadow")
        self.assertIsNone(report["guarded_result"])
        self.assertFalse(report["parent_review"])
        # Legacy shape callers keep reading the unchanged deterministic route.
        self.assertEqual(report["result"]["decision_route"], "TEAM_FIX")

    def test_invalid_policy_mode_rejected_before_any_network_call(self):
        opener = FakeOpener(FakeResponse(make_response()))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            with self.assertRaises(AdvisorError) as ctx:
                run_advise(case_path, 0.01, SECRET, opener=opener, policy_mode="bogus")
        self.assertEqual(ctx.exception.code, "policy_mode_invalid")
        self.assertEqual(opener.requests, [])

    def test_guarded_mode_promotes_clear_technical_lead_above_threshold(self):
        opener = FakeOpener(FakeResponse(make_response(
            answers={"route": make_answer("architect", GUARDED_PROMOTION_THRESHOLD)})))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case(
                category="technical_design", reproduced=False, restores_baseline=False))
            report = run_advise(case_path, 0.01, SECRET, opener=opener, policy_mode="guarded")
        self.assertEqual(report["policy_mode"], "guarded")
        # Baseline (legacy shape) is untouched.
        self.assertEqual(report["result"]["decision_route"], "LEAD")
        self.assertEqual(report["guarded_result"]["baseline_decision_route"], "LEAD")
        self.assertEqual(report["guarded_result"]["decision_route"], "ARCHITECT")
        self.assertTrue(report["guarded_result"]["applied"])
        self.assertFalse(report["parent_review"])
        self.assertFalse(report["guarded_result"]["authorization"])

    def test_guarded_mode_conflict_keeps_baseline_and_flags_parent_review(self):
        # baseline_reference missing -> LEAD, not TEAM_FIX; a "self" claim at
        # high confidence would bypass that missing evidence.
        opener = FakeOpener(FakeResponse(make_response(
            answers={"route": make_answer("self", 0.99)})))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case(baseline_reference=""))
            report = run_advise(case_path, 0.01, SECRET, opener=opener, policy_mode="guarded")
        self.assertEqual(report["result"]["decision_route"], "LEAD")
        self.assertEqual(report["guarded_result"]["decision_route"], "LEAD")
        self.assertFalse(report["guarded_result"]["applied"])
        self.assertTrue(report["guarded_result"]["parent_review"])
        self.assertTrue(report["parent_review"])

    def test_guarded_mode_owner_signal_against_team_fix_flags_parent_review(self):
        opener = FakeOpener(FakeResponse(make_response(
            answers={"route": make_answer("owner", 0.99)})))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case())
            report = run_advise(case_path, 0.01, SECRET, opener=opener, policy_mode="guarded")
        self.assertEqual(report["result"]["decision_route"], "TEAM_FIX")
        self.assertEqual(report["guarded_result"]["decision_route"], "TEAM_FIX")
        self.assertFalse(report["guarded_result"]["applied"])
        self.assertTrue(report["guarded_result"]["parent_review"])
        self.assertTrue(report["parent_review"])


    def test_guarded_mode_falls_back_to_baseline_when_jev_unavailable(self):
        opener = FakeOpener(FakeResponse(make_response()))
        with tempfile.TemporaryDirectory() as tmp:
            case_path = write_case(tmp, make_case(
                category="technical_design", reproduced=False, restores_baseline=False))
            report = run_advise(case_path, 0.0, SECRET, opener=opener, policy_mode="guarded")
        self.assertEqual(report["jev_consultation"]["status"], "unavailable")
        self.assertEqual(report["guarded_result"]["decision_route"], "LEAD")
        self.assertFalse(report["guarded_result"]["applied"])
        self.assertFalse(report["parent_review"])


class WriteNewTests(unittest.TestCase):
    def test_output_cannot_overwrite_existing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            write_new(str(path), {"a": 1})
            with self.assertRaises(AdvisorError) as ctx:
                write_new(str(path), {"a": 2})
            self.assertEqual(ctx.exception.code, "output_exists")
            self.assertEqual(json.loads(path.read_text()), {"a": 1})


class LoadCaseTests(unittest.TestCase):
    def test_rejects_oversized_case_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.json"
            path.write_text(json.dumps({"pad": "x" * (70 * 1024)}))
            with self.assertRaises(AdvisorError) as ctx:
                load_case(str(path))
            self.assertEqual(ctx.exception.code, "case_too_large")

    def test_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json")
            with self.assertRaises(AdvisorError) as ctx:
                load_case(str(path))
            self.assertEqual(ctx.exception.code, "case_invalid_json")


if __name__ == "__main__":
    unittest.main()
