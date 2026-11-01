# Hermes 0.22.2 side-by-side upgrade — fixed installation operator

Bounded upgrade of the AI Lab Hermes standalone installation to the exact
pinned release **0.22.2** (commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8`).
The pinned commit is the only accepted source identity; there is no 1:1
provider-cloud mapping in this procedure. No install, no download, no
network: the parent stages the source side-by-side under the pinned
release directory and the operator only verifies, swaps, and rolls back
the launcher against that staged tree, reusing the EXISTING interpreter.

Accepted deployment operator (this installation only):

- `docs/stack-adaptations-20261019/fixed_hermes_deployment.py`

The earlier-round research helper and its synthetic-root test, retained
under `global/codex/skills/hermes-delegation/scripts/`, are NOT the
accepted operator, are NOT advertised, and MUST NOT be invoked against
this installation. The accepted operator is intentionally narrower than
the research helper and is not a reusable generic installer.

## What this operator guarantees

- ONLY mutable target on the host: `ROOT/bin/hermes` (regular file; symlinks
  and root aliases refused before any read or write).
- The operator does NOT stage source. Staging is the parent's job. The
  operator only verifies what the parent already laid down:
  `git -C <release> rev-parse HEAD` must equal the pinned SHA, the only
  tracked delta versus `HEAD` must be `agent/agent_runtime_helpers.py`,
  the SHA of that file must equal `OPS/staged-hardening-sha256.txt`, and
  the release tree must not contain untracked or ignored `.py`/`.pth`/
  `.so`/`.js` files. The operator never reads `VERSION`/`COMMIT` files;
  those are staging artefacts, not gates.
- Reused as-is: `ROOT/install/hermes-agent/venv/bin/python`,
  `ROOT/home/.hermes`, `ROOT/home/.codex`, original `HOME`/`state.db`/
  sessions/auth, shared GPU files, `/usr/local/bin/hermes`, the user outer
  wrapper, the Native Desktop path, and the delegation worker.
- The generated launcher exports `HERMES_ROOT`, `HERMES_HOME`,
  `CODEX_HOME`, `HERMES_INSTALL_DIR`, then puts the staged source FIRST on
  `sys.path` via an inline `sys.path.insert(0, ...)` preamble so the existing
  interpreter runs staged code (never the old installed editable copy).
- Source-first Python import is the contract; `python -m` (which would let
  caller cwd win over `PYTHONPATH`) is NOT used.
- Atomic launcher replacement via temp file in the operator dir
  (`OPS`) + `os.replace`; temp file retained on disk (no auto-clean) per
  task constraints; `fsync`/`fchmod` before replace; fresh preimage backup
  re-read and hash-verified; old launcher SHA re-verified immediately
  before the swap.
- `observe` always writes the latest evidence to
  `OPS/fixed-operator-observe.json` via the same atomic helper; the report
  path is symlink-checked so a stale `fixed-operator-*.json` symlink that
  points at the launcher slot cannot redirect the write. The symlink
  gate on the report path runs in `main()` BEFORE `change()` so a
  pre-existing report symlink that resolves to the launcher slot fails
  the whole run with no launcher mutation.
- The untracked-source gate uses two `git ls-files -z` passes
  (`--others --ignored --exclude-standard` and
  `--others --exclude-standard`), splits on NUL, and decodes each field
  with `os.fsdecode` before `Path.suffix`. `Path(bytes)` is not supported.
  The parent reproduced `TypeError` for that call on Linux. Without `-z`, Git
  C-quotes unusual names, so a Unicode name such as `tools/évil.py` or a
  newline name such as `tools/new\nline.py` is wrapped in quotes and the
  suffix is no longer `.py`. After decode, `.py`, `.pth`, `.so`, and
  `.js` are refused. `.txt` and `.pyc` are allowed.

## Operator constants (NOT configurable)

- `ROOT = /srv/ai-lab/hermes-standalone`
- `PIN  = d923a5eb487a17c0a68801bba9ef0f56c81040a8`
- `RELEASE = ROOT/releases/hermes-0.22.2-d923a5eb487a` (12-char prefix of
  `PIN`, not `d923a5eb48`)
- `PY = ROOT/install/hermes-agent/venv/bin/python`
- `OPS = ROOT/ops/upgrade-0222-20261019`
- `OLD_SHA = a76d028c6e917b9e132f3f469a9e9822dee204254b8ecb15e57c72265e74ec40` —
  hard-coded constant inside the operator. The operator takes no flags
  and accepts no CLI argument for `expected_old_sha`; the constant must
  match the preimage bytes captured when the parent staged the release.

## Closed operation set

| CLI arg | Effect |
|---|---|
| *(no arg)* | Run `observe`: read the live launcher, compute the candidate launcher bytes against the pinned release/interpreter, and persist SHA evidence to `OPS/fixed-operator-observe.json`. `observe` does NOT validate the release tree (no pin check, no hardening fingerprint, no interpreter check) and does NOT touch `ROOT/bin/hermes`. Pin and hardening are enforced by `activate`, not by `observe`. |
| `activate` | Validate the staged release (`git HEAD == PIN`, only the allowed tracked edit, no untracked/ignored executable source, hardening SHA matches, interpreter present), drift-check the current launcher against `OLD_SHA`, write a hash-verified preimage backup, persist recovery metadata, then atomic-replace the launcher. Idempotent only when the launcher bytes, mode `old_mode|0o111`, meta `pin`, and backup hash all agree. |
| `rollback` | Restore the retained preimage bytes and `old_mode`; idempotent only when both already match. `rollback` does NOT call `validate_release()` and does NOT require the staged release tree to be intact — only the retained meta, the retained backup, and the target/ops symlink gates enforced inside `change()`. |

The operator has NO `inspect-release`, `validate`, `harden`, `render-launcher`
or arbitrary-runner subcommand. `validate_release()` runs for `activate`
only; the only hardening the operator recognises is the exact bytes
fingerprinted in `OPS/staged-hardening-sha256.txt` (created by the parent
when it staged the release). The operator will refuse `activate` if that
fingerprint does not match the staged `agent/agent_runtime_helpers.py`.

## Activation invariants (fail-closed)

`activate` refuses to replace the launcher if any of the following is true. A backup or meta file may already exist from this attempt; these refusals do not rewrite the JSON report:

- Root, release dir, release `agent/`, operator dir, or recovery files are
  reachable only through a symlink (`managed symlink refused`).
- `git -C <release> rev-parse HEAD` is not the pinned SHA (`wrong upstream
  commit`).
- `git diff --name-only HEAD` is not exactly
  `agent/agent_runtime_helpers.py`. That compares the working tree with
  HEAD, so staged and unstaged tracked edits both count, and the
  hardening edit must stay uncommitted so HEAD remains the pin
  (`unexpected tracked source changes`).
- `OPS/staged-hardening-sha256.txt` does not match the SHA of the staged
  hardening file (`hardening changed since native canary`).
- A NUL-delimited `git ls-files -z` name, after `os.fsdecode`, has suffix
  `.py`, `.pth`, `.so`, or `.js`, including Unicode names and names that
  contain a newline (`untracked executable source in release`). `.txt`
  and `.pyc` are allowed.
- The pre-existing interpreter is missing.
- The live launcher SHA-256 is neither `OLD_SHA` nor the candidate
  (`launcher drift; no write`). Bytes that already equal the candidate
  take the idempotent path instead of this drift refusal.
- The retained backup (if present) does not equal `OLD_SHA`
  (`backup mismatch`).
- A fresh preimage backup just written cannot be re-read back as
  `OLD_SHA` (`fresh backup hash mismatch`).
- The launcher is already in the target state but the retained meta+backup
  disagree with it (`inconsistent retained activation`). The retained
  meta's `pin` field must equal this operator's `PIN`; an activation
  written by a different operator/pin is refused as inconsistent so the
  same launcher bytes produced by an unrelated pin cannot be presented
  as our idempotent success and immediately rejected on rollback.
- The launcher is already in the target state but its mode is not the
  post-activation mode (`idempotent activation mode mismatch`).
  `atomic()` writes the new launcher with `old_mode | 0o111`, so the
  expected mode is `m['old_mode'] | 0o111`. An externally-stripped mode
  (e.g. `0o644` while the activation wrote `0o755`) is refused so a
  non-executable launcher cannot be reported as a successful idempotent
  activation.

`rollback` will refuse if the retained meta or backup is missing, if the
retained material does not match `OLD_SHA` / pin, or if the current
launcher was changed by anyone other than this operator (`foreign launcher
change; no rollback`). `rollback` does NOT require the staged release tree
to be intact — `validate_release()` is activation-only. When the
launcher is already in the rolled-back byte state, `rollback` additionally
verifies that the current mode equals the saved `m['old_mode']`
(`idempotent rollback mode mismatch`) so an externally-stripped mode on a
byte-correct rolled-back launcher is refused and not reported as a
successful idempotent rollback.

## Rehearsal

The accepted operator is bound to the production `ROOT`: it takes no
flags, no `--root` argument, and exposes no fixture or synthetic-root
mode. Operator rehearsal is therefore parent-only and runs against the
production tree (with the canary/retained-preimage controls already
required for the real switch). It is NOT in this contractor's scope.

Retained research under the skill `scripts/` directory is not this
operator and must not be run against this installation. It is not
evidence of native tool use, persistence, process restart, or of this
operator's launcher swap. Parent evidence for the pinned tree is
`task-inputs/stack-adaptations/HERMES-NATIVE-EVIDENCE.md`: stage-only
`--version` reported Hermes Agent v0.22.2 with activation still false,
and the synthetic loopback transport PASS is not model inference.

## Production switch (parent only, Mateusz-authorized)

The parent alone performs the real stage, the canary, and the rollback. The
contractor MUST NOT activate, roll back, or otherwise invoke this operator
against `/srv/ai-lab/hermes-standalone`.

1. Stage the pinned source under
   `ROOT/releases/hermes-0.22.2-d923a5eb487a/` so the release directory name
   matches the operator's `PIN[:12]`. The operator does NOT read `VERSION`
   or `COMMIT` files; the only staging artefacts it consumes are
   `git -C <release> rev-parse HEAD` (must equal `d923a5eb487a17c0a68801bba9ef0f56c81040a8`),
   the staged `agent/agent_runtime_helpers.py`, and the SHA recorded in
   `OPS/staged-hardening-sha256.txt`.
2. `python docs/stack-adaptations-20261019/fixed_hermes_deployment.py` —
   `observe` writes `OPS/fixed-operator-observe.json`. The report contains
   `before_sha256`, `candidate_sha256`, `expected_old_sha256` and `mode`;
   it does NOT contain pin or hardening SHA. The parent must additionally
   confirm `git -C <release> rev-parse HEAD == d923a5eb487a17c0a68801bba9ef0f56c81040a8`
   and that `OPS/staged-hardening-sha256.txt` matches the staged
   `agent/agent_runtime_helpers.py` before invoking `activate`.
   `candidate_sha256` must differ from `before_sha256`.
3. `python docs/stack-adaptations-20261019/fixed_hermes_deployment.py activate` —
   `OLD_SHA` is the hard-coded constant inside the operator (no CLI flag
   is accepted). A release-gate failure leaves the launcher bytes
   unchanged. Drift means the live SHA-256 is neither `OLD_SHA` nor the
   candidate, and the swap is refused. A failure after the backup or
   meta write but before `os.replace` of the launcher also leaves the
   preimage bytes in the slot. Read the launcher, backup, and meta;
   "no write" is not a safe summary of every refusal.
4. Whether `activate` exits zero or raises, determine the state from the
   live launcher bytes and mode, `OPS/launcher-preimage.sh`, and
   `OPS/launcher-activation.json`, as below. Do not use the JSON report
   or the exit code alone. An offline version string of Hermes Agent
   v0.22.2 for this pin is a launcher check, not a model call. Do not run
   a delegation smoke, a long-turn task, or a provider request: Codex and
   Grok tokens are expired, and the model canary is `OWNER_AUTH_PENDING`.
   Leave shared GPU files untouched.
5. On regression:
   `python docs/stack-adaptations-20261019/fixed_hermes_deployment.py rollback`
   restores the retained preimage bytes and `old_mode`. Read the launcher
   bytes, mode, backup, and meta again. A repeated rollback is a no-op
   only when those bytes and the mode already match the retained meta.

## Model readiness is NOT a part of this operator

The synthetic native tool / persistence / restart evidence is a loopback
transport PASS: a native file tool ran, the same session resumed, and
production history stayed untouched. It does not prove this operator's
launcher swap and it is not model inference.

No `activate` or `rollback` result is claimed here. The parent performs
that sequence on the host; the contractor does not rehearse it. As of
this writing the existing Codex/Grok tokens have expired and the
actual model canary is `OWNER_AUTH_PENDING`. The operator must not be
presented as evidence of model readiness, and the runbook must not claim
inference success until Mateusz has re-authorised the live model backend.

## Failure surface

The operator raises `ValueError` (exit code 1) with one of the messages
quoted in *Activation invariants* above. There is no generic shell, no
`shell=True`, no process kill, no arbitrary command runner, and no second
daemon/store/GPU/history copy.

Early validation exceptions leave the earlier JSON report unchanged.
The report write can fail after launcher replacement. Determine the
actual state from the current launcher bytes and mode and from the
retained backup and meta, not from a stale JSON report or the exit code
alone. There is no second reporting channel.

`OPS/fixed-operator-<action>.json` is written only at the end of `main()`,
after `validate_release()` when the action is `activate`, and after
`change()` for every action:

- If `validate_release()` raises — pin, hardening, untracked source,
  interpreter, or any release gate failed — the launcher is untouched
  and no report write runs; the previous report file (if any) is left
  in place. An old success-shaped report from an earlier round therefore
  survives a failed `activate` and must NOT be read as today's outcome.
- If `change()` raises — drift, backup mismatch, fresh-backup hash
  mismatch, symlink refusal inside `change()`, foreign launcher, etc. —
  no report write runs and the previous report file is left in place.
  On `activate`, backup creation (only when the backup was absent) and
  the meta file `OPS/launcher-activation.json` happen before `os.replace`
  of `ROOT/bin/hermes`. A raise inside that launcher `atomic()`, before
  replace returns, leaves the preimage bytes in the slot. Meta may
  already be new, so meta alone does not prove the slot moved. On
  `rollback`, the restore is the last write inside `change()`. A raise
  before that replace leaves the previous launcher bytes in place and
  does not rewrite the JSON report. A raise while re-reading the slot
  after a successful replace also skips the report write, with the
  launcher already changed.
- If `atomic(report, ...)` itself raises — disk full, permission change,
  ENOSPC on the OPS filesystem — that failure happens AFTER `change()`
  has already returned. For `activate` that means the launcher is on
  the new pinned release but the report is either stale or absent, so
  a non-zero exit does not prove the launcher is untouched. Read launcher
  bytes, mode, retained backup, and meta. For `rollback`, a failed report
  write means the restore is already on disk while the report file is
  still the earlier one.

On every attempt, read the live launcher bytes (`sha256sum ROOT/bin/hermes`),
the live mode (`stat -c %a ROOT/bin/hermes`), the retained backup
(`OPS/launcher-preimage.sh`, SHA-256 versus `OLD_SHA`), and the retained
meta (`OPS/launcher-activation.json` fields `pin`, `old_sha256`,
`new_sha256`, `old_mode`). Landed activation means the launcher SHA-256
equals meta `new_sha256`, `pin` is `d923a5eb487a17c0a68801bba9ef0f56c81040a8`,
the backup SHA-256 and meta `old_sha256` are `OLD_SHA`, and the live mode
is `old_mode|0o111`. Preimage or rolled back means the launcher SHA-256
and the backup SHA-256 are `OLD_SHA` and the live mode equals meta
`old_mode`. Anything else is drift. The JSON report is not this state. A pre-existing
`OPS/fixed-operator-<action>.json` symlink that resolves to the launcher
slot raises `managed symlink refused: <report path>` and the operator
exits before `change()` mutates the launcher, so the production slot is
never written through a redirected report path. The report path is
computed in `main()` and gated up front for every action (`observe`,
`activate`, `rollback`).