# Synthetic dry-run fixture

This directory is a **deterministic, never-executed** fixture. The test
suite reads its `manifest.json` to confirm that the new
`simulate-attack` skill ships with a synthetic target reference,
but the suite never invokes the helper against this fixture and never
opens a network connection, subprocess, or external service here.

The fixture does not contain production, customer, billing, mail,
calendar, payment, CRM, or public-integration data. The
`target_class` is `TEST` and the `executed` flag is `false`.
