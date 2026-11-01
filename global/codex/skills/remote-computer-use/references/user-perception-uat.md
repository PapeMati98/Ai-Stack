# User-perception UAT on the isolated VM

Use these scenarios when `remote-computer-use` supplies evidence to
`accept-system`. They are mandatory PASS/FAIL scenarios for every applicable
user-facing workflow. The runner supplies bounded interaction primitives; the
application or test fixture must supply deterministic state and fault seams.

## Surface reachability

1. Start from the contract's viewport or desktop work area.
2. Open each window, modal, drawer, menu, picker, popover and status surface in
   the primary journey.
3. Capture its visible bounds, critical content, primary action, close path and
   focus state.
4. Where the target supports it, repeat after a safe test-only change of
   resolution, scale/DPI, monitor topology or stale saved window coordinates.

PASS only when the surface and its controls remain reachable and focus returns
to a logical element. An off-screen or uncloseable surface is FAIL.

## Perceived action feedback

1. Trigger each material action once.
2. Record the first visible accepted/pending cue and elapsed time against the
   product-specific threshold frozen by the acceptance contract.
3. Verify duplicate submission protection for non-idempotent actions.
4. Record the true terminal success, failure or cancellation and correlate it
   with system evidence.

PASS requires both a prompt perceptible cue and a truthful terminal state. A
still-looking button, false success, duplicate effect or permanent spinner is
FAIL.

## Visible failure and recovery

Use an existing test seam to trigger each applicable condition: validation,
offline, timeout, 401/403, 404, 409, 429, 5xx and background-job failure. Verify
that the user sees a contextual and actionable message, retains entered data
and orientation, and can retry or correct the action safely. Correlate the UI
with redacted network/backend evidence.

Console-only errors, disappearing dialogs, lost input, false success and
failure without recovery are FAIL. If no safe deterministic fault seam exists,
return BLOCKED; never target production or weaken the VM policy to force one.

## Evidence row

Record: role, surface/route, initial viewport or work area, zoom/scale/DPI and
monitor state when applicable, action trace, injected condition, expected cue,
observed cue, focus state, recovery path, system correlation, artifact paths,
reviewer and verdict. Screenshots alone cannot prove timing or backend truth.

Privacy mode may prohibit screenshots or text entry. In that mode use the
allowed assertions and redacted network evidence; do not widen capture or
input permissions for a prettier report.
