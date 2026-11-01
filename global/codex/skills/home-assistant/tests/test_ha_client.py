#!/usr/bin/env python3
"""Offline regression tests for ha_client (no network, no credentials)."""
import json
import sys
import os
import unittest
from urllib.parse import urlparse

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts")))
import ha_client as H


def fake_ok(responses):
    def fn(method, url, headers, data, timeout):
        assert "Bearer" in headers.get("Authorization", "")
        assert "HASS_TOKEN" not in headers.get("Authorization", "")
        key = (method, url.split("/api", 1)[-1] if "/api" in url else url)
        if key in responses:
            status, body = responses[key]
            return status, body if isinstance(body, str) else json.dumps(body)
        return 404, "{}"
    return fn


class CredsTest(unittest.TestCase):
    def test_missing_auth(self):
        with self.assertRaises(H.HAError):
            H.read_credentials(env={})

    def test_alias_env(self):
        url, tok = H.read_credentials(env={"HA_URL": "http://h:8123",
                                            "HA_TOKEN": "t"})
        self.assertEqual(url, "http://h:8123")

    def test_url_rejects_userinfo_query_fragment(self):
        for bad in ("ftp://h/x", "http://user@h/", "http://@h/",
                    "http://user:pass@h:8123/", "http://h/?a=1",
                    "http://h/#f", "http:///"):
            with self.assertRaises(H.HAError):
                H.validate_base_url(bad)

    def test_origin_rebuilt_from_scheme_host_port_only(self):
        self.assertEqual(H.validate_base_url("http://h:8123/"),
                         "http://h:8123")
        self.assertEqual(H.validate_base_url("https://h/x"),
                         "https://h")

    def test_divergent_base_url_flag_refuses_env_auth(self):
        env = {"HASS_URL": "http://home:8123",
               "HASS_TOKEN": "sekret"}
        real_environ = os.environ
        try:
            os.environ = dict(real_environ, **env)
            rc = H.main(["--base-url", "http://evil:9999", "health"])
        finally:
            os.environ = real_environ
        self.assertEqual(rc, 1)

    def test_secret_redaction(self):
        s = "Authorization: Bearer abc123 and token=xyz q"
        r = H.redact(s)
        self.assertNotIn("abc123", r)
        self.assertNotIn("xyz", r)


class EntityTest(unittest.TestCase):
    def test_malformed_and_broad_rejected(self):
        for bad in ("all", "light.all", "group.all_lights", "*",
                    "Light Bad", "light.bad!", ""):
            with self.assertRaises(H.HAError):
                H.validate_entity_id(bad)

    def test_only_light_services(self):
        self.assertEqual(H.validate_light_service("light.turn_on"),
                         "light.turn_on")
        for bad in ("light.toggle", "switch.turn_on", "homeassistant.restart"):
            with self.assertRaises(H.HAError):
                H.validate_light_service(bad)

    def test_resolve_all_only_light_domain(self):
        states = [{"entity_id": "light.a"}, {"entity_id": "switch.a"},
                  {"entity_id": "light.b"}, {"entity_id": "group.all_lights"}]
        self.assertEqual(H.resolve_light_targets(states, ["all"]),
                         ["light.a", "light.b"])

    def test_safe_state_drops_sensitive_attrs(self):
        raw = {"entity_id": "light.a", "state": "on",
               "attributes": {"friendly_name": "A",
                              "access_token": "SECRET",
                              "entity_picture": "http://cam/x.jpg",
                              "latitude": 1.0}}
        out = H.safe_state(raw)
        blob = json.dumps(out)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("entity_picture", blob)
        self.assertNotIn("latitude", blob)


class RedirectTest(unittest.TestCase):
    def test_redirect_handler_blocks(self):
        h = H.NoRedirect()
        with self.assertRaises(H.HAError):
            h.redirect_request(None, None, 302, "Found", {}, "http://evil/")

    def test_opener_ignores_proxy_env(self):
        import urllib.request
        opener = H.build_opener(10.0)
        proxies = {}
        for h in opener.handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                proxies = h.proxies
        self.assertEqual(proxies, {})


class LightsFlowTest(unittest.TestCase):
    def test_dry_run_default_no_post(self):
        calls = []

        def fn(method, url, headers, data, timeout):
            calls.append(method)
            return 200, "{}"

        r = H.set_lights("http://h:8123", "tok", ["light.a"],
                         "light.turn_on", execute=False, request_fn=fn,
                         poll_delay=0)
        self.assertTrue(r["dry_run"])
        self.assertEqual(calls, [])
        self.assertEqual(r["planned"], ["light.a"])
        self.assertEqual(r["accepted"], [])
        self.assertFalse(r["partial"])

    def test_partial_verification_unknown_state(self):
        resp = {
            ("POST", "/services/light/turn_on"): (200, "{}"),
            ("GET", "/states"): (200, [
                {"entity_id": "light.a", "state": "on", "attributes": {}},
                {"entity_id": "light.b", "state": "mystery",
                 "attributes": {}},
            ]),
        }
        r = H.set_lights("http://h:8123", "tok", ["light.a", "light.b"],
                         "light.turn_on", execute=True, request_fn=fake_ok(resp),
                         poll_delay=0)
        self.assertEqual(r["verified"], ["light.a"])
        self.assertEqual(r["unknown"], ["light.b"])
        self.assertTrue(r["partial"])
        self.assertFalse(r["delivery_unknown"])

    def test_max_lights_bound(self):
        with self.assertRaises(H.HAError):
            H.set_lights("http://h:8123", "tok",
                         ["light.a%d" % i for i in range(50)],
                         "light.turn_on", execute=False, max_lights=5,
                         request_fn=lambda *a: (200, "{}"))


class InferenceTest(unittest.TestCase):
    NOW = 1_000_000

    def test_absent_and_stale_is_unknown(self):
        self.assertEqual(
            H.infer_availability(self.NOW)["recommendation"], "UNKNOWN")
        self.assertEqual(
            H.infer_availability(self.NOW, last_activity_ts=1,
                                 stale_s=100)["recommendation"], "UNKNOWN")

    def test_recent_activity_is_in(self):
        r = H.infer_availability(self.NOW, last_activity_ts=self.NOW - 60,
                                 activity_observed_ts=self.NOW - 60)
        self.assertEqual(r["recommendation"], "IN")

    def test_night_light_off_after_debounce_may_suggest_out(self):
        r = H.infer_availability(self.NOW, last_activity_ts=self.NOW - 2000,
                                 activity_observed_ts=self.NOW - 120,
                                 light_on=False, light_ts=self.NOW - 2000,
                                 person_seen=False,
                                 person_ts=self.NOW - 2000,
                                 night=True)
        self.assertEqual(r["recommendation"], "OUT")

    def test_person_seen_without_fresh_evidence_stays_unknown(self):
        r = H.infer_availability(self.NOW, last_activity_ts=self.NOW - 2000,
                                 person_seen=True, night=False)
        self.assertEqual(r["recommendation"], "UNKNOWN")

    def test_stale_light_evidence_stays_unknown(self):
        r = H.infer_availability(self.NOW, last_activity_ts=self.NOW - 2000,
                                 activity_observed_ts=self.NOW - 60,
                                 light_on=False, light_ts=1,
                                 person_seen=False,
                                 person_ts=self.NOW - 60,
                                 night=True)
        self.assertEqual(r["recommendation"], "UNKNOWN")

    def test_stale_or_missing_person_context_still_out(self):
        base = dict(last_activity_ts=self.NOW - 2000,
                    activity_observed_ts=self.NOW - 60,
                    light_on=False, light_ts=self.NOW - 60, night=True)
        for person_seen, person_ts in ((True, 1), (None, self.NOW - 60),
                                       (None, None), (False, 1),
                                       (True, None)):
            r = H.infer_availability(self.NOW, person_seen=person_seen,
                                     person_ts=person_ts, **base)
            self.assertEqual(r["recommendation"], "OUT",
                             msg=repr((person_seen, person_ts)))

    def test_home_person_context_never_changes_verdict(self):
        base = dict(last_activity_ts=self.NOW - 2000,
                    activity_observed_ts=self.NOW - 60,
                    light_on=False, light_ts=self.NOW - 60, night=True)
        recs = set()
        for person_seen, person_ts in ((True, self.NOW - 60),
                                       (False, self.NOW - 60),
                                       (None, self.NOW - 60),
                                       (True, 1), (None, None)):
            r = H.infer_availability(self.NOW, person_seen=person_seen,
                                     person_ts=person_ts, **base)
            recs.add(r["recommendation"])
        self.assertEqual(recs, {"OUT"})

    def test_string_night_is_not_night(self):
        r = H.infer_availability(self.NOW, last_activity_ts=self.NOW - 2000,
                                 activity_observed_ts=self.NOW - 60,
                                 light_on=False, light_ts=self.NOW - 60,
                                 person_seen=True,
                                 person_ts=self.NOW - 60,
                                 night="false")
        self.assertEqual(r["recommendation"], "UNKNOWN")

    def test_manual_override_wins(self):
        r = H.infer_availability(self.NOW, last_activity_ts=self.NOW - 10,
                                 light_on=True, night=False, manual="OUT")
        self.assertEqual(r["recommendation"], "OUT")

    def test_output_is_evidence_only(self):
        r = H.infer_availability(self.NOW, last_activity_ts=self.NOW - 60)
        self.assertIn("evidence", r)
        self.assertNotIn("mutate", json.dumps(r).lower())


class OwnerScenarioTest(unittest.TestCase):
    """Owner at home asleep: lights off, fresh 2h-idle observation."""

    NOW = 1720000000.0

    def test_at_home_asleep_advisory_out(self):
        r = H.infer_availability(
            self.NOW, last_activity_ts=self.NOW - 7200,
            activity_observed_ts=self.NOW - 120,  # freshly observed
            light_on=False, light_ts=self.NOW - 120,
            person_seen=True, person_ts=self.NOW - 120,  # presence not veto
            night=True)
        self.assertEqual(r["recommendation"], "OUT")
        self.assertTrue(r["evidence"]["person_seen"])
        self.assertEqual(r["evidence"]["activity_age_s"], 7200)

    def test_missing_activity_observation_is_unknown(self):
        # No fresh idle observation at all: last timestamp alone is stale.
        r = H.infer_availability(
            self.NOW, last_activity_ts=self.NOW - 7200,
            activity_observed_ts=None,
            light_on=False, light_ts=self.NOW - 100,
            person_seen=True, person_ts=self.NOW - 100,
            night=True)
        self.assertEqual(r["recommendation"], "UNKNOWN")

    def test_stale_activity_observation_is_unknown(self):
        r = H.infer_availability(
            self.NOW, last_activity_ts=self.NOW - 7200,
            activity_observed_ts=self.NOW - 7200,  # stale, re-read needed
            light_on=False, light_ts=self.NOW - 100,
            person_seen=True, person_ts=self.NOW - 100,
            night=True)
        self.assertEqual(r["recommendation"], "UNKNOWN")

    def test_bad_timestamps_are_unknown(self):
        for bad in (True, "1720000000", float("nan"), float("inf"),
                    self.NOW + 5):
            r = H.infer_availability(
                self.NOW, last_activity_ts=bad,
                activity_observed_ts=self.NOW - 10,
                light_on=False, light_ts=self.NOW - 10,
                person_seen=False, person_ts=self.NOW - 10,
                night=True)
            self.assertEqual(r["recommendation"], "UNKNOWN", msg=str(bad))

    def test_missing_person_reading_still_suggests_out(self):
        # Home person data is optional context only: missing HOME data
        # must not block the office-availability OUT suggestion.
        for person_seen, person_ts in ((None, self.NOW - 60),
                                       (None, None),
                                       (True, 1),
                                       (False, self.NOW - 60),
                                       (True, self.NOW - 60)):
            r = H.infer_availability(
                self.NOW, last_activity_ts=self.NOW - 7200,
                activity_observed_ts=self.NOW - 60, light_on=False,
                light_ts=self.NOW - 60, person_seen=person_seen,
                person_ts=person_ts, night=True)
            self.assertEqual(r["recommendation"], "OUT",
                             msg=repr((person_seen, person_ts)))

    def test_manual_wins(self):
        r = H.infer_availability(
            self.NOW, last_activity_ts=self.NOW - 7200,
            activity_observed_ts=self.NOW - 60, manual="OUT",
            light_on=False,
            light_ts=self.NOW - 60, person_seen=True,
            person_ts=self.NOW - 60, night=True)
        self.assertEqual(r["recommendation"], "OUT")
        r2 = H.infer_availability(
            self.NOW, last_activity_ts=self.NOW - 5,
            activity_observed_ts=self.NOW - 5, manual="OUT")
        self.assertEqual(r2["recommendation"], "OUT")


class BatchCallTest(unittest.TestCase):
    def test_one_post_one_get(self):
        resp = {
            ("POST", "/services/light/turn_on"): (200, "{}"),
            ("GET", "/states"): (200, [
                {"entity_id": "light.a", "state": "on", "attributes": {}},
                {"entity_id": "light.b", "state": "on", "attributes": {}},
            ]),
        }
        calls = []

        def fn(method, url, headers, data, timeout):
            calls.append((method, urlparse(url).path))
            return fake_ok(resp)(method, url, headers, data, timeout)

        r = H.set_lights("http://h:8123", "tok",
                         [" light.A ", "light.b", "light.a", "light.B"],
                         "light.turn_on", execute=True,
                         request_fn=fn, poll_delay=0)
        self.assertEqual(r["post_calls"], 1)
        self.assertEqual(r["snapshot_calls"], 1)
        self.assertEqual(r["accepted"], ["light.a", "light.b"])
        self.assertEqual(r["verified"], ["light.a", "light.b"])
        self.assertFalse(r["delivery_unknown"])

    def test_unavailable_and_unknown_mixed(self):
        resp = {
            ("POST", "/services/light/turn_on"): (200, "{}"),
            ("GET", "/states"): (200, [
                {"entity_id": "light.a", "state": "on", "attributes": {}},
                {"entity_id": "light.b", "state": "unavailable",
                 "attributes": {}},
                {"entity_id": "light.c", "state": "mystery",
                 "attributes": {}},
            ]),
        }
        r = H.set_lights("http://h:8123", "tok",
                         ["light.a", "light.b", "light.c"],
                         "light.turn_on", execute=True,
                         request_fn=fake_ok(resp), poll_delay=0)
        self.assertEqual(r["verified"], ["light.a"])
        self.assertEqual(r["unavailable"], ["light.b"])
        self.assertEqual(r["unknown"], ["light.c"])
        self.assertTrue(r["partial"])

    def test_post_failure_never_erases(self):
        def boom(method, url, headers, data, timeout):
            p = urlparse(url).path
            if method == "POST":
                raise RuntimeError("net down")
            return fake_ok({})(method, url, headers, data, timeout)

        r = H.set_lights("http://h:8123", "tok", ["light.a"],
                         "light.turn_on", execute=True,
                         request_fn=boom, poll_delay=0)
        self.assertEqual(r["accepted"], [])
        self.assertEqual(r["unknown"], ["light.a"])
        self.assertTrue(r["delivery_unknown"])
        self.assertIn("error", r)

    def test_snapshot_failure_keeps_evidence(self):
        resp = {("POST", "/services/light/turn_on"): (200, "{}")}

        def fn(method, url, headers, data, timeout):
            if method == "GET":
                raise RuntimeError("readback down")
            return fake_ok(resp)(method, url, headers, data, timeout)

        r = H.set_lights("http://h:8123", "tok", ["light.a"],
                         "light.turn_on", execute=True,
                         request_fn=fn, poll_delay=0)
        self.assertEqual(r["accepted"], ["light.a"])
        self.assertEqual(r["unknown"], ["light.a"])
        self.assertTrue(r["delivery_unknown"])
        self.assertIn("error", r)

    def test_domain_and_duplicates_validated(self):
        with self.assertRaises(H.HAError):
            H.set_lights("http://h:8123", "tok", ["switch.a"],
                         "light.turn_on")
        with self.assertRaises(H.HAError):
            H.set_lights("http://h:8123", "tok", ["light.a"],
                         "switch.turn_on")
        r = H.set_lights("http://h:8123", "tok", ["light.a", "light.a"],
                         "light.turn_on", execute=False)
        self.assertEqual(r["planned"], ["light.a"])
        self.assertTrue(r["dry_run"])


class ProxyDisabledTest(unittest.TestCase):
    def test_real_build_opener_ignores_http_proxy(self):
        # urllib drops a ProxyHandler({}) from the chain by design
        # (no per-protocol xxx_open methods are created for empty
        # proxies) and build_opener skips the default env-reading
        # ProxyHandler as a duplicate class. Net effect: zero proxy
        # handlers, so the Bearer never leaves via a proxy. Assert
        # that absence-of-route, not handler presence.
        try:
            saved = dict(os.environ)
            os.environ["HTTP_PROXY"] = "http://proxy.invalid:8080"
            os.environ["http_proxy"] = "http://proxy.invalid:8080"
            os.environ["HTTPS_PROXY"] = "http://proxy.invalid:8080"
            os.environ["https_proxy"] = "http://proxy.invalid:8080"
            import urllib.request
            self.assertTrue(urllib.request.getproxies(),
                            "env must look hostile for this test")
            op = H.build_opener(timeout=1)
            ph = [h for h in op.handlers
                  if isinstance(h, urllib.request.ProxyHandler)]
            for h in ph:
                self.assertEqual(h.proxies, {})
            routed = [h for handlers in op.handle_open.values()
                      for h in handlers
                      if isinstance(h, urllib.request.ProxyHandler)]
            self.assertEqual(routed, [])
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
