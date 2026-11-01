# Fleet0.9.142 / RSI b482d3

See RELEASE-0.9.142.md.11 new home-isolation regressions; RED5fail/3error on141,47focused+96adjacentPASS. Full2646base/2657candidate retain28failures and49/50errors; the extra OpenClaw timeout reproduces on unchanged141. Not a green full suite. Final installer, independent ACCEPT, deployment, backup and native evidence: task CLOSEOUT.md.

# Fleet0.9.141 / RSI ce7fa0

Final frozen patch:121 focused PASS,20 installer PASS; RED9tests/7assertions/0errors.
Full2646 (26fail/48error) vs2637base (27fail/48error): no new failed IDs.
Not a green full suite. See RELEASE-0.9.141.md and task evidence.

# Fleet 0.9.133 - RSI dc5dea

See RELEASE-0.9.133.md. Targeted58PASS; RED3/18 on132; full2600 vs2594 with no new failed IDs, same41 failures/102 errors/30 skips under retained offline fixtures. Independent ACCEPT: run-20261024T044409Z-e488a57e8f.

# Fleet 0.9.132 - RSI HD-20261015-4175c0

Benign pytest cache (`.pytest_cache`) handling during Fleet runtime attestation:
- Root cause: verifier included `.pytest_cache/v/cache/nodeids` in manifest; initial valid runtime verification then failed fingerprint mismatch.
- Fix: `.pytest_cache` added to `IGNORED_DIRS` of both `verify_runtime.py` and `install_router_suite.py`.
- Security constraints:
  - Cache directories must never hide executable source code. `EXECUTABLE_SOURCE_SUFFIXES` (`.bat`, `.cmd`, `.com`, `.cjs`, `.exe`, `.js`, `.mjs`, `.ps1`, `.py`, `.sh` in casefold) are strictly refused inside `.pytest_cache`.
  - Cache directories must never hide filesystem links. Links, junctions, and reparse points inside `.pytest_cache` are strictly refused before any ignore continue.
  - Guard ordering runs before broader `__pycache__` logic to avoid nested cache bypasses (e.g. `__pycache__/.pytest_cache` or `.pytest_cache/__pycache__`).
  - Legacy behavior for existing `__pycache__` and `.ruff_cache` is preserved.
  - Normal payload SHA256/count and runtime marker checks remain intact.

Validation (`scripts/test_runtime_pytest_cache.py`):
- Package root resolved dynamically via `Path(__file__).resolve().parents[1]` for honest RED baseline replay against unchanged 0.9.131.
- Root and nested benign `.pytest_cache` under sealed runtime preserves fingerprint and passes verification.
- Root nested cache mixed with legacy ignored directories (`__pycache__`, `.ruff_cache`) preserves fingerprint for benign cache and refuses executable source and links.
- All 10 prohibited executable suffixes (`.bat`, `.cmd`, `.com`, `.cjs`, `.exe`, `.js`, `.mjs`, `.ps1`, `.py`, `.sh`) tested in both lowercase and uppercase for both verifier and installer.
- Exact paths outside cache including `.pytest_cache`-looking prefixes (`.pytest_cache_tamper`, `.pytest_cache.backup`, `pytest_cache`, `.pytest_cache-fake`, `prefix_.pytest_cache`) change fingerprint.
- Marker tampering and normal payload tampering are strictly refused.
- Mocked `is_filesystem_link` at cache root, nested files, and nested directories verifies link refusal.
- Mocked Windows symlinks verify link refusal; real NTFS junctions tested if supported without changing privileges.
- Manifest parity verified between `installer.tree_manifest` and `verifier.runtime_manifest` on identical synthetic cache-bearing payloads.
- Operating isolation: all tests execute strictly inside disposable temporary directories outside the installed runtime. Never test inside `~/.agents/runtime`. Use `TEAM_TEST_ROOT` pointing at a task-owned disposable fixture root outside the installed runtime when a fixed test root is needed. No hardcoded machine evidence paths.

# Fleet 0.9.114 - RSI HD-20261013-906853

0.9.114 = sealed 0.9.113 plus one correction in `mistral_runtime.py` `read_native_session_stats`: the real Vibe 2.26.1 session root holds `.last_session/` and `active/` beside `session_<id>/`, so the 0.9.113 "exactly one directory" reader always returned `None` on real runs. 0.9.114 picks the directory with a regular `meta.json` (newest mtime wins). See RELEASE-0.9.114.md.

Validation (same no-delete harness as below): new module 30/30 GREEN on 0.9.114, the two layout tests RED on the unchanged 0.9.113 copy; focused modules 174/174; full suite 2390 tests, same 44 pre-existing failing ids (plus one occurrence of the pre-existing `test_timeout_interaction` timing flake under concurrent load, 3/3 PASS alone); offline application of the reader to the two real 0.9.113 canary folders returns the expected stats; independent delta review Antigravity gemini-3.8-flash-high run-20261020T061616Z-212aa32fa4 ACCEPT, 0 findings. Real 0.9.113 canaries: control COMPLETED (run-20261020T060930Z-73df5a95c1), stall PARTIAL `mistral-native-progress-stall` (run-20261020T061228Z-d0d25a0274), both with `mistral_native_session: null`, which is the defect this release corrects; the installed 0.9.114 canaries (test module, real control and stall runs with the stats present) are recorded in the evidence folder and the Hub handoff.

# Fleet 0.9.113 - RSI HD-20261013-906853

Native Mistral Vibe progress stall: Fleet now stops a `mistral` run that shows no native progress (tool telemetry, session `messages.jsonl`, `vibe.log` mtimes) for longer than `FLEET_MISTRAL_STALL_SECONDS` (default 1200 s, `0` disables, no effect when the parent timeout is not longer) with the same PARTIAL / retained-artifact semantics as a parent timeout, completion reason `mistral-native-progress-stall`, and names the content-free native session stats (steps, completion tokens, last step seconds, idle seconds) in the diagnosis and the run record, also when the process was killed and no native export exists. Product changes only in `process_supervision.py`, `mistral_runtime.py` and `fleet.py` (Mistral path; other adapters pass no probe); see RELEASE-0.9.113.md.

Validation (all through a no-delete harness with `TEAM_TEST_ROOT` in `agent-stack-worktrees/_do-usuniecia-20261020/rsi-mistral-906853`, cleanup paths retained):

- New `test_mistral_progress_stall.py`: 28 tests GREEN on 0.9.113; RED on the unchanged 0.9.112 copy (35 failed ids with subtests: the probe/stall APIs are absent there).
- Focused Mistral/supervision/diagnosis modules (`test_mistral_runtime`, `test_mistral_native_stop_diagnosis`, `test_mistral_budget_diagnosis`, `test_mistral_session_budget`, `test_mistral_tools`, `test_context_supervision`, `test_fleet_diagnosis_cause` plus the new module): 172/172 PASS, twice in a row; the unchanged copy 144/144 twice. `test_context_supervision.test_timeout_interaction` (0.3 s child timeout) failed once under CPU load in the maker's round 2 and once in the first post-correction run, then passed 3/3 alone and 2/2 in the suite on both copies: a pre-existing timing flake, not a candidate defect.
- Full component suite: 2360 -> 2388 tests, the same 44 pre-existing failure/error ids, 0 new, 2 pre-existing skips. Other component test debt remains; this release does not claim a fully green suite.
- Maker: Antigravity gemini-3.8-flash-high through `todo_pair` (runs run-20261020T044129Z-5ca32d2a7e, run-20261020T050842Z-6b08330600, run-20261020T053301Z-1e8ac61ef4; rounds 2-3 repaired test-side expectations only). Independent review of the whole candidate: Grok 4.7 run-20261020T054340Z-8964fe2e8d ACCEPT, 0 findings. Parent trivial corrections after that review (ASCII-only stall env value, non-finite floats dropped from the session stats, CRLF/LF restored to each file's base convention) were re-reviewed independently by Antigravity gemini-3.8-flash-high run-20261020T060251Z-30271828fb ACCEPT, 0 findings; the reviewed and corrected copies are kept in the evidence folder.
- Installed canaries after `install_router_suite.py deploy --no-delete`: the new module on the installed `lean-0.9.113` scripts, then two real Mistral runs from the installed runtime in a synthetic worktree folder: a control advisory run with the default limit (must complete as before) and a provocation with `FLEET_MISTRAL_STALL_SECONDS=2` (must end PARTIAL `mistral-native-progress-stall` with the new diagnosis). Results are recorded in the evidence folder and the final Hub handoff.

Evidence: `<task-owned-evidence-root>` (checks.py harness, base-full/candidate-full/full-comparison.json, red-replay.json, candidate.diff, parent-corrections.diff, review outputs, canary results). Release 0.9.111 remains reserved by an unrelated candidate. Rollback: 0.9.112 and lean-0.9.112 stay; `install_router_suite.py rollback --receipt <deployment receipt>` or re-plan/deploy sealed 0.9.112 `--no-delete`. `FLEET_MISTRAL_STALL_SECONDS=0` disables the stall stop without redeploy. Never push agent-stack. Private shared-skills backup PUSHED/CURRENT is required.
