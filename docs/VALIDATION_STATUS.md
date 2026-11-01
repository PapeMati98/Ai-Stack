# Validation status

This source edition is not a supported general release. The evidence below is
bounded to the maintainer's Windows and Python 3.13 journey. The Fleet source
version is 0.9.142; the Office package and runtime versions are 0.1.9 and
0.2.0 respectively.

| Area | Evidence and boundary |
|---|---|
| Native Graph journey | An actual exported Graph used native makers and independent reviewers. Two dependent products were accepted after three real repairs; a controlled fault went from RED to pair ACCEPT. The journey included 24 meaningful cases. |
| Offline reference | Six original synthetic files are copied byte for byte. Their 24 offline cases comprise 12 library, 6 CLI, and 6 review-regression cases. These checks test the reference packaging separately from the native journey. |
| Office | The original installer reported CURRENT on nine targets, and an isolated send/read returned ACK. This is not a production rollout claim. |
| Knowledge Hub | A nested Git root/vault handoff reached COMPLETE/CHECKPOINTED. The corrected source Scout was EMPTY with zero calls; no reviewed history was fabricated. |
| RSI Helpdesk | An isolated queue entry was filed and re-read. Autonomous repair was not demonstrated. |
| delivery-audit | Source-only and unsupported in a fresh profile: legacy Fleet/Graph 0.9.82 pins and missing HiddenLaunch 1.0.3 prevent qualification. It fails closed; this is not a production PASS. |

## Public edition checks

This public edition renames the Polish skill names to English, replaces client
and host names with fictional placeholders, scopes the OpenRouter API key in
`video-gen` and `audio-bed-gen` to the OpenRouter API origin, and leaves out the
Microsoft 365 workspace connector (see [SECURITY.md](../SECURITY.md)).

Every `test_*.py` file was run separately with pytest on Windows and Python
3.13, both on the unmodified private source and on this edition. About 520 of
663 files pass. The others also fail on the unmodified source: they need the
maintainer's installed runtimes, CLIs or accounts, or they time out. Files that
differed between the two parallel runs were re-run one at a time; the only real
difference was fixed. A clean checkout on another machine will show a
different, environment-dependent set of failures.

There is no qualification of all 13 CLIs or all operating systems, autonomous
RSI repair, a production deployment, or a security certification. Source
inclusion does not transfer accounts or subscriptions. The repository is
licensed under the Apache License 2.0 (see `LICENSE` and `NOTICE` in the
repository root); third-party components keep their own notices.

See the [offline reference](../examples/delivery-evidence/README.md) for
reproducible local commands and the scope of those checks.
