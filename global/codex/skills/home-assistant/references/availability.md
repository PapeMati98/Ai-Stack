# Availability inference (advisory, no mutation)

Function: infer_availability(now_ts, last_activity_ts=None,
light_on=None, light_ts=None, person_seen=None, person_ts=None,
activity_observed_ts=None, manual=None, night=False, debounce_s=900,
stale_s=3600). Two distinct clocks: last_activity_ts is the last
human input/command (owner intent signal); activity_observed_ts is
when the observation itself was taken. A fresh observation of a quiet
house is distinct from recent human activity and must never be
mistaken for it. light_on and person_seen are undated readings and
never decide alone; their light_ts/person_ts timestamps carry the age.

Configuration example (NOT policy): debounce_s=900, stale_s=3600.
Activation is pending owner policy; do not treat these numbers as
decided business timeouts.

Rules:

- manual IN/OUT/UNKNOWN wins over everything.
- invalid now_ts -> UNKNOWN.
- missing, negative, or stale activity_observed_ts (observation age
  > stale_s) -> UNKNOWN. A fresh idle observation is required; it is
  distinct from last_activity_ts. Missing or stale observation never
  yields IN or OUT.
- activity age unknown, negative, or in the future -> UNKNOWN.
- activity within debounce_s -> IN.
- absent or stale light_ts evidence -> UNKNOWN (a bare light_on
  value with no fresh timestamp never decides).
- home person_seen/person_ts is OPTIONAL CONTEXT ONLY: HOME true,
  false, missing, or stale must all leave the verdict identical; it
  neither forces IN, blocks OUT, nor gates OUT. Night is strict
  (night is True; string "false" never counts as night).
- night (strict True) + office light off (light_on False with fresh
  light_ts) + no activity past debounce + fresh activity observation
  -> OUT (a suggestion, still evidence only). person_seen=True does
  NOT block OUT: the presence sensor is context only (e.g. owner at
  home asleep still yields OUT as an availability suggestion).
- otherwise UNKNOWN.

The three concepts stay separate:

- physical home presence: where the body is (sensors only suggest).
- human availability: whether Mateusz can be interrupted (advised).
- decision authority: who may act (owner only; never this function).

Output is recommendation + evidence. It never writes to Office,
never assumes home presence, and never authorizes an action.
