#!/usr/bin/env python3
"""Portable Home Assistant client (Python 3 standard library only).

Offline-safe: all HTTP goes through an injectable request function so unit
tests run without network. Reads credentials from process environment only:
HASS_URL / HASS_TOKEN (aliases HA_URL / HA_TOKEN). Never reads browser
auth stores, never writes secret files, never prints credentials.

Usage (fallback client; prefer native ha_* tools when available):
  python ha_client.py health
  python ha_client.py state light.kitchen
  python ha_client.py find kitchen
  python ha_client.py services
  python ha_client.py lights-on light.a light.b --execute
  python ha_client.py lights-off light.a --execute
Without --execute every write is a dry run.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.2.0"

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
ALLOWED_LIGHT_SERVICES = {"light.turn_on": ("light", "turn_on"),
                           "light.turn_off": ("light", "turn_off")}
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_LIGHTS = 20
DEFAULT_POLL_DELAY = 0.5
SAFE_STATE_KEYS = ("entity_id", "state", "friendly_name", "last_changed",
                   "last_updated")
SECRET_RE = re.compile(r"(?i)(bearer\s+[A-Za-z0-9\-._~+/=]+|"
                       r"token[\"']?\s*[:=]\s*[\"']?[^\"'\s,}]+)")


def redact(text):
    return SECRET_RE.sub("[REDACTED]", str(text))


def fail(message):
    sys.stderr.write(redact(message) + "\n")
    return 1


class HAError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HAError("redirect blocked to keep auth private")


def read_credentials(env=None):
    env = env if env is not None else os.environ
    url = env.get("HASS_URL") or env.get("HA_URL") or ""
    token = env.get("HASS_TOKEN") or env.get("HA_TOKEN") or ""
    if not url or not token:
        raise HAError("missing auth: set HASS_URL and HASS_TOKEN "
                      "(or HA_URL/HA_TOKEN) in process environment")
    return url.strip(), token


def validate_base_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        raise HAError("invalid base URL")
    if parts.scheme not in ("http", "https"):
        raise HAError("base URL must be http(s)")
    if not parts.hostname:
        raise HAError("base URL must have a host")
    if "@" in parts.netloc:
        raise HAError("base URL must not embed userinfo")
    if parts.username or parts.password:
        raise HAError("base URL must not embed userinfo")
    if parts.query or parts.fragment:
        raise HAError("base URL must not carry query/fragment")
    host = parts.hostname
    origin = parts.scheme + "://" + host
    if parts.port:
        origin += ":%d" % parts.port
    return origin


def validate_entity_id(entity_id):
    eid = (entity_id or "").strip().lower()
    if eid in ("all", "light.all", "group.all_lights", "*"):
        raise HAError("broad target rejected: pass an explicit entity list")
    if not ENTITY_RE.match(eid):
        raise HAError("malformed entity_id: %s" % entity_id)
    return eid


def validate_light_service(service):
    svc = (service or "").strip().lower()
    if svc not in ALLOWED_LIGHT_SERVICES:
        raise HAError("only light.turn_on / light.turn_off are supported")
    return svc


def build_opener(timeout):
    # Empty ProxyHandler: never honor HTTP_PROXY/HTTPS_PROXY, so the
    # Authorization Bearer token is never handed to a proxy that could
    # forward it past NoRedirect on the next hop.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       NoRedirect)


def api_request(base_url, token, method, path, payload=None,
                timeout=DEFAULT_TIMEOUT, request_fn=None):
    base = validate_base_url(base_url)
    if not token:
        raise HAError("missing auth")
    url = base.rstrip("/") + path
    data = None
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json"}
    if payload is not None:
        try:
            data = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError):
            raise HAError("malformed payload")
    if request_fn is not None:
        return request_fn(method, url, headers, data, timeout)
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    opener = build_opener(timeout)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except HAError:
        raise
    except urllib.error.HTTPError as exc:
        raise HAError("HA HTTP error %s" % exc.code)
    except Exception:
        raise HAError("HA request failed (offline or unreachable)")


def safe_state(raw):
    """Compact allowlisted state. Drops attributes that may carry tokens,
    camera URLs, or location."""
    attrs = raw.get("attributes", {}) if isinstance(raw, dict) else {}
    return {
        "entity_id": raw.get("entity_id", "unknown"),
        "state": raw.get("state", "unknown"),
        "friendly_name": attrs.get("friendly_name", ""),
        "last_changed": raw.get("last_changed", ""),
        "last_updated": raw.get("last_updated", ""),
    }


def pick_fields(raw):
    return {k: safe_state(raw).get(k) for k in SAFE_STATE_KEYS}


def resolve_light_targets(states, requested):
    """All-lights resolution: only entities whose domain is exactly light.
    Never switches, groups, or scenes. `requested` may contain the word
    'all' meaning every current domain-light entity."""
    lights = [s.get("entity_id", "") for s in states
              if str(s.get("entity_id", "")).startswith("light.")]
    if len(requested) == 1 and requested[0].strip().lower() == "all":
        return sorted(set(lights))
    out = [validate_entity_id(e) for e in requested]
    for eid in out:
        if not eid.startswith("light."):
            raise HAError("lights operation needs light.* entities")
    return out


def snapshot_states(base_url, token, timeout, request_fn=None):
    """One bounded readback snapshot: a single GET /api/states.

    Returns (states_by_id, error). states_by_id maps lowercased entity_id
    to safe_state. error is None on success, else a redacted message.
    A failed snapshot never erases known effects; callers keep `accepted`
    and report unverified targets as unknown with delivery_unknown set.
    """
    try:
        status, body = api_request(base_url, token, "GET", "/api/states",
                                   timeout=timeout, request_fn=request_fn)
    except HAError as exc:
        return {}, redact(str(exc))
    except Exception:
        return {}, "snapshot GET failed"
    try:
        items = json.loads(body)
    except ValueError:
        return {}, "snapshot GET returned malformed JSON"
    if not isinstance(items, list):
        return {}, "snapshot GET returned unexpected shape"
    got = {}
    for raw in items:
        if isinstance(raw, dict) and raw.get("entity_id"):
            got[str(raw["entity_id"]).strip().lower()] = safe_state(raw)
    return got, None


def check_states(base_url, token, entity_ids, timeout, request_fn=None):
    """Per-entity readback kept for single-entity callers; batch flows
    must use snapshot_states (one GET) instead."""
    got = {}
    for eid in entity_ids:
        status, body = api_request(base_url, token, "GET",
                                   "/api/states/" + eid,
                                   timeout=timeout, request_fn=request_fn)
        try:
            raw = json.loads(body)
        except ValueError:
            raw = {}
        got[eid] = safe_state(raw)
    return got


def set_lights(base_url, token, entity_ids, service, execute=False,
               timeout=DEFAULT_TIMEOUT, max_lights=DEFAULT_MAX_LIGHTS,
               poll_delay=DEFAULT_POLL_DELAY, request_fn=None):
    """Batch light control: exactly ONE service POST for all targets, then
    exactly ONE bounded snapshot GET for readback.

    Domain validation happens in-function: every target must validate as
    an entity_id and belong to the light.* domain (duplicates collapse to
    one entry, order preserved). Non-light or malformed targets raise
    HAError before any network call.

    Returns per-target accepted / verified / unavailable / unknown lists.
    delivery_unknown is True when the snapshot readback failed, meaning
    acceptance is known but delivery is unverified -- known effects are
    never erased after a network/GET failure.
    """
    svc = validate_light_service(service)
    raw_targets = [validate_entity_id(e) for e in entity_ids]
    targets = []
    for eid in raw_targets:
        if eid not in targets:
            targets.append(eid)
    if not targets:
        raise HAError("empty entity list: selection is required")
    for eid in targets:
        if not eid.startswith("light."):
            raise HAError("lights operation needs light.* entities, "
                          "rejected: %s" % eid)
    if len(targets) > max_lights:
        raise HAError("too many targets (max %d)" % max_lights)
    domain, action = ALLOWED_LIGHT_SERVICES[svc]
    plan = {"service": svc, "targets": targets, "execute": bool(execute)}
    if not execute:
        return {"dry_run": True, "plan": plan, "planned": list(targets),
                "accepted": [], "verified": [], "unavailable": [],
                "unknown": [], "partial": False, "delivery_unknown": False,
                "post_calls": 0, "snapshot_calls": 0}
    try:
        status, _ = api_request(
            base_url, token, "POST",
            "/api/services/%s/%s" % (domain, action),
            {"entity_id": targets}, timeout=timeout, request_fn=request_fn)
    except HAError as exc:
        return {"dry_run": False, "plan": plan, "accepted": [],
                "verified": [], "unavailable": [], "unknown": list(targets),
                "partial": True, "delivery_unknown": True,
                "error": redact(str(exc)), "post_calls": 1,
                "snapshot_calls": 0}
    except Exception:
        return {"dry_run": False, "plan": plan, "accepted": [],
                "verified": [], "unavailable": [], "unknown": list(targets),
                "partial": True, "delivery_unknown": True,
                "error": "light POST failed", "post_calls": 1,
                "snapshot_calls": 0}
    accepted = list(targets)
    if poll_delay:
        time.sleep(min(poll_delay, 5.0))
    actual, snap_error = snapshot_states(base_url, token, timeout,
                                         request_fn)
    want = "on" if action == "turn_on" else "off"
    verified, unavailable, unknown = [], [], []
    if snap_error is not None:
        unknown = list(targets)
    else:
        for eid in targets:
            st = actual.get(eid, {}).get("state", "unknown")
            if st == want:
                verified.append(eid)
            elif st in ("unavailable", "none"):
                unavailable.append(eid)
            else:
                unknown.append(eid)
    done = set(verified)
    partial = any(e not in done for e in accepted)
    out = {"dry_run": False, "plan": plan, "accepted": accepted,
           "verified": verified, "unavailable": unavailable,
           "unknown": unknown, "partial": partial,
           "delivery_unknown": snap_error is not None,
           "post_calls": 1, "snapshot_calls": 1}
    if snap_error is not None:
        out["error"] = snap_error
    return out


def _valid_ts(value):
    """Accept only finite numeric timestamps (rejects bool/str/NaN/inf)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _age(now_ts, ts):
    if not _valid_ts(now_ts) or not _valid_ts(ts):
        return None
    return now_ts - ts


def infer_availability(now_ts, last_activity_ts=None, light_on=None,
                       light_ts=None, person_seen=None, person_ts=None,
                       activity_observed_ts=None, manual=None, night=False,
                       debounce_s=900, stale_s=3600):
    """Pure advisory inference. Dated inputs only; no I/O, no mutation.

    Two distinct clocks:
    - last_activity_ts: last human input/command (owner intent signal).
    - activity_observed_ts: freshness of the observation itself (when the
      state snapshot or event that produced these inputs was taken). A
      fresh observation of a quiet house still means the house is quiet;
      it must never be mistaken for recent human activity.

    Undated light_on booleans alone never decide; missing or stale light
    or observation evidence returns UNKNOWN. Home person_seen/person_ts
    is optional context only: it never forces IN, blocks OUT, or gates
    a verdict. A presence sensor is not human availability:
    person_seen=True never forces or blocks a verdict on its own.
    Manual IN/OUT/UNKNOWN always wins. Returns
    dict(recommendation, evidence) where recommendation is IN, OUT, or
    UNKNOWN (evidence only).
    """
    evidence = {
        "now": now_ts,
        "last_activity_ts": last_activity_ts,
        "activity_observed_ts": activity_observed_ts,
        "light_on": light_on,
        "light_ts": light_ts,
        "person_seen": person_seen,
        "person_ts": person_ts,
        "manual": manual,
        "night": night,
    }
    if manual in ("IN", "OUT", "UNKNOWN"):
        return {"recommendation": manual,
                "evidence": dict(evidence, reason="manual override wins")}
    if not _valid_ts(now_ts):
        return {"recommendation": "UNKNOWN",
                "evidence": dict(evidence,
                                 reason="invalid now timestamp")}
    obs_age = _age(now_ts, activity_observed_ts)
    evidence["observation_age_s"] = obs_age
    if obs_age is None or obs_age < 0 or obs_age > stale_s:
        return {"recommendation": "UNKNOWN",
                "evidence": dict(evidence,
                                 reason="missing or stale observation; "
                                        "fresh idle state is distinct "
                                        "from last human input")}
    age = _age(now_ts, last_activity_ts)
    evidence["activity_age_s"] = age
    if age is None:
        return {"recommendation": "UNKNOWN",
                "evidence": dict(evidence,
                                 reason="no dated last-activity evidence")}
    if age < 0:
        return {"recommendation": "UNKNOWN",
                "evidence": dict(evidence,
                                 reason="last-activity timestamp is in "
                                        "the future")}
    if age <= debounce_s:
        return {"recommendation": "IN",
                "evidence": dict(evidence, reason="recent activity")}
    light_age = _age(now_ts, light_ts)
    person_age = _age(now_ts, person_ts)
    evidence["light_age_s"] = light_age
    evidence["person_age_s"] = person_age
    # Dated readings only: a light value without a fresh timestamp is
    # missing evidence. Home person_seen/person_ts is optional context
    # only: it is recorded in evidence but never gates the verdict.
    light_ok = light_on is not None and light_age is not None \
        and 0 <= light_age <= stale_s
    if not light_ok:
        return {"recommendation": "UNKNOWN",
                "evidence": dict(evidence,
                                 reason="absent or stale light evidence")}
    # Quiet house at night, office lights off, input idle past debounce:
    # OUT is suggested regardless of home presence data. person_seen
    # True/False/missing/stale is context only (e.g. owner at home
    # asleep still yields OUT as an availability suggestion); it must
    # neither force IN, block OUT, nor require its presence/timestamp.
    if night is True and light_on is False:
        return {"recommendation": "OUT",
                "evidence": dict(evidence,
                                 reason="no input for %ds, office light "
                                        "off at night after debounce; "
                                        "presence sensor is context only"
                                        % int(age))}
    return {"recommendation": "UNKNOWN",
            "evidence": dict(evidence, reason="no decisive signal")}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ha_client.py",
                                 description="Offline-capable HA fallback "
                                             "client (dry-run by default).")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--max-lights", type=int, default=DEFAULT_MAX_LIGHTS)
    ap.add_argument("--poll-delay", type=float, default=DEFAULT_POLL_DELAY)
    ap.add_argument("--execute", action="store_true",
                    help="actually call services; otherwise dry run")
    ap.add_argument("command", nargs="?",
                    choices=["health", "state", "find", "services",
                             "lights-on", "lights-off"])
    ap.add_argument("args", nargs="*")
    ns = ap.parse_args(argv)
    if not ns.command:
        ap.print_help()
        return 2
    try:
        timeout = min(max(ns.timeout, 1.0), 60.0)
        max_lights = min(max(ns.max_lights, 1), 100)
        poll = min(max(ns.poll_delay, 0.0), 5.0)
        env_url, env_token = read_credentials()
        env_origin = validate_base_url(env_url)
        if ns.base_url:
            flag_origin = validate_base_url(ns.base_url)
            if flag_origin != env_origin:
                raise HAError("refusing divergent --base-url: auth "
                              "is pinned to HASS_URL/HA_URL origin")
        base_url = env_origin
        token = env_token
        if ns.command == "health":
            status, body = api_request(base_url, token, "GET", "/api/",
                                       timeout=timeout)
            print(json.dumps({"ok": status == 200, "status": status}))
        elif ns.command == "state":
            if not ns.args:
                raise HAError("state needs one entity_id")
            eid = validate_entity_id(ns.args[0])
            status, body = api_request(base_url, token, "GET",
                                       "/api/states/" + eid, timeout=timeout)
            print(json.dumps(safe_state(json.loads(body))))
        elif ns.command == "find":
            if not ns.args:
                raise HAError("find needs a query")
            q = ns.args[0].lower()
            status, body = api_request(base_url, token, "GET",
                                       "/api/states", timeout=timeout)
            items = json.loads(body)
            out = [safe_state(r) for r in items
                   if q in str(r.get("entity_id", "")).lower()
                   or q in str(r.get("attributes", {}).get(
                       "friendly_name", "")).lower()][:25]
            print(json.dumps(out))
        elif ns.command == "services":
            status, body = api_request(base_url, token, "GET",
                                       "/api/services", timeout=timeout)
            items = json.loads(body)
            out = [{"domain": d.get("domain", ""),
                    "services": sorted(d.get("services", {}).keys())}
                   for d in items]
            print(json.dumps(out))
        elif ns.command in ("lights-on", "lights-off"):
            svc = ("light.turn_on" if ns.command == "lights-on"
                   else "light.turn_off")
            if len(ns.args) == 1 and ns.args[0].lower() == "all":
                status, body = api_request(base_url, token, "GET",
                                           "/api/states", timeout=timeout)
                targets = resolve_light_targets(json.loads(body), ["all"])
            else:
                targets = [validate_entity_id(e) for e in ns.args]
                for eid in targets:
                    if not eid.startswith("light."):
                        raise HAError("lights operation needs light.*")
            result = set_lights(base_url, token, targets, svc,
                                execute=ns.execute, timeout=timeout,
                                max_lights=max_lights, poll_delay=poll)
            print(json.dumps(result))
        return 0
    except HAError as exc:
        return fail("error: %s" % exc)
    except Exception:
        return fail("error: unexpected failure")


if __name__ == "__main__":
    sys.exit(main())
