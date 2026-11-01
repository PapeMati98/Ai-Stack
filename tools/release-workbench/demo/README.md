# Executable offline demo and parent native UAT

Python 3.11+ and a local Git executable are required. No packages, network,
installed model binaries, live business state or global config changes are needed
for the offline demo. Run from the workbench root. Every output path must be new;
use a fresh workspace/report revision after a failure. Failed artifacts remain.

The following Windows commands use explicit task-owned paths. `out` must already
exist; replace the example workspace/report names if they already exist.

```text
python -B -m release_workbench --help
python -B -m release_workbench demo --source demo/synthetic-source.md --workspace "<absolute-path-to-your-checkout>/out/demo-01" --report out/demo-01-report.json
python -B -m release_workbench verify --candidate out/demo-01/candidate-a --receipt out/demo-01/receipt-a.json
python -B -m release_workbench scan --candidate out/demo-01/candidate-a --report out/demo-01/scan-v2.json
python -B -m release_workbench config-check --config out/demo-01/config.json --owned-root "<absolute-path-to-your-checkout>/out/demo-01"
python -B -m release_workbench rights-check --manifest out/demo-01/manifest.json --components out/demo-01/components.json --report out/demo-01/rights-v2.json
python -B -m release_workbench bundle-check --candidate out/demo-01/candidate-a --receipt out/demo-01/receipt-a.json --report out/demo-01/bundle-v2.json
python -B -m unittest discover -s tests
```

`demo` creates a synthetic Git repository, commits only the supplied fixture,
exports that committed blob twice, compares full receipts, calls the actual
configuration, privacy, portability and rights APIs, and writes notices and a
bundle report outside both candidate trees. It returns `PASS` for the offline
journey, `NOT_RUN` for native UAT, `UNKNOWN` for product acceptance and
`public_ready: false`. Rights stay `OPEN` and bundle status stays `PENDING`.
Git commit IDs may differ between independent demos; exports of the same pinned
commit have identical receipts. The fixture is not a replacement AI Stack.

Exit codes: 0 for completed operations/preparation (`PASS`, `exported`, `verified`,
`PREPARED`, `EXECUTED`); 1 for errors/`FAIL`/`BLOCKED`; 2 for `OPEN`/`PENDING` gates.
All operational output is JSON. `--help` uses argparse help text.
Existing report paths are refused without overwriting. Choose a new path such as
`scan-v3.json`. Reports cannot be written into a candidate's exact inventory.
No publish, push, license-grant or approval-token command exists.

## Native UAT: prepare first, execute only after parent review

The driver does not fabricate Graph/Fleet/Office/Hub/RSI implementations. The
parent supplies an explicitly exported candidate/receipt, secret-free config,
native plan and a **separate committed Git workspace with its own `.git`
directory**. Neither directory may contain the other. Home/profile/agent-state
locations and linked worktrees are refused. No host knowledge/Office config is
loaded. Config's stack/state/runtime paths are distinct relative directories
under that workspace; enabled CLIs identify existing approved accounts.

```text
python -B -m release_workbench native-uat --candidate EXPLICIT_CANDIDATE --receipt RECEIPT.json --config CONFIG.json --workspace ABSOLUTE_SEPARATE_GIT_WORKSPACE --plan PLAN.json --report NEW_PREPARATION_REPORT.json
python -B -m release_workbench native-uat --candidate EXPLICIT_CANDIDATE --receipt RECEIPT.json --config CONFIG.json --workspace ABSOLUTE_SEPARATE_GIT_WORKSPACE --plan PLAN.json --report NEW_EXECUTION_REPORT.json --execute
```

Preparation checks inputs and Git identity, but executes no exported Python
program and creates no workspace files. Missing dependencies produce a precise
`BLOCKED` stage. Preparation cannot prove OS isolation or provider availability.
The parent runs `--execute` after independent code acceptance, inside controlled
resources. Execution creates `native-config.json` once, invokes the exported
runner for each declared stage, and stops at the first failed/blocked stage or
timeout. New attempts require a fresh workspace, retaining previous evidence.
No automatic retry, alternate implementation, alternate model or host fallback
is available. Escalate repeated transport timeouts to the parent rather than
starting a third attempt.

### Plan protocol (version 1)

The exact top-level keys are `version`, `tree_hash`, `roles`, `isolation`, `stages`.
`tree_hash` binds the exact verified receipt. `roles` has `maker` and `reviewer`,
each with `agent`, `model`, `lineage` identifier strings. Agent/model pairs must be
enabled in the config. Equal agent, model or declared lineage is refused.
These declarations require independent verification; spelling a different
lineage does not establish independence.

`isolation` has an exported Python `entrypoint` and `args` list. It must be an
actual reviewed OS containment launcher from the candidate, not a token file,
environment-only shim, transcript fixture or private installed implementation.
It must confine its children from creation, enforce no private business-state or
production access, prevent daemon/child escape on timeout, and launch descendants
windowlessly. The workbench's own subprocesses use `CREATE_NO_WINDOW` on Windows.
If no such runner has been exported and reviewed, native execution is **BLOCKED**;
this workbench does not implement a new OS sandbox.

The runner argv must contain each exact token `{entrypoint}`, `{args_json}`,
`{workspace}`, `{config}`, `{evidence}`, `{stage}`. These are substituted with the
real mechanism path, its JSON argv list, the explicit workspace, newly written
config path, new stage evidence path and stage ID. `{candidate}` is also allowed.
Literal arguments cannot name absolute/remote/home/production paths or use shell
expansion. Tokens must occupy a whole argument. No shell or Python `-c` commands
are synthesized. Runner entrypoint and all five mechanisms must be distinct
receipt-listed `.py` source files. Entrypoint hashes are retained.

`stages` contains exactly five entries in dependency order: `office`, `hub`,
`rsi`, `graph`, `fleet`. Each has exactly:

```json
{
  "id": "office",
  "entrypoint": "exported/path/to/real_office_entry.py",
  "args": ["--workspace", "{workspace}", "--config", "{config}"],
  "role": "maker",
  "expected": "isolated-office-roundtrip",
  "evidence": "office-evidence.json",
  "timeout_s": 120
}
```

These are schema examples, not claims about the original mechanism's flag names.
The parent supplies its reviewed actual arguments. Evidence paths are distinct,
new workspace-relative paths with existing parent directories. Timeouts are
1..240 seconds. Graph must exercise the maker; Fleet must exercise the independent
reviewer. Each stage must run the real exported mechanism against isolated local
resources, capture real run IDs/identity/checks and any repair. The runner must
make the expected bindings below available to its evidence recorder.

The process environment is rebuilt from runtime/system paths, private HOME/XDG/
temporary locations and Git isolation controls. Ambient API keys and Office/Hub
variables are dropped. Native providers can legitimately access existing
authorized OS authentication and system libraries through the reviewed runner;
the driver never copies or reads auth files. A provider requiring unavailable
profile-based auth must report the exact blocked dependency. HOME redirection
alone does not prove absence of private business-state access.

### Evidence protocol (version 1)

The exported runner creates the requested bounded JSON file with exactly:

- `version`, `stage`, `status` (`PASS`, `FAIL`, `BLOCKED`), `role`, `expected`.
- `actual`: exactly `agent`, `model`, `lineage`, `run_id`, `result` identifiers.
  Identity must equal the declared stage role. Record actual provider output,
  not an invented run identifier when a provider has not responded. For an
  incomplete stage, `run_id` may be `null` and `result` names the blocked dependency
  (for example `native-auth-unavailable`); agent/model/lineage then describe the
  requested role, not a measured provider identity.
- `environment`: `workspace_git_head` matching preparation,
  `private_business_state: "DENIED"`, `production_endpoints: "DENIED"`, and
  `native_auth: "OS_AUTH_PERMITTED"` or `"NOT_USED"`.
  Incomplete stages can truthfully report `NOT_ESTABLISHED` for isolation fields
  and `UNAVAILABLE` for auth. Those values never support `PASS`.
- `hashes`: exactly `tree_hash`, `config_sha256`, `plan_sha256`,
  `isolation_runner_sha256`, `entrypoint_sha256`. Config/plan digests use UTF-8
  `json.dumps(sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2)` plus
  one LF; source/artifact digests hash original bytes.
- `checks`: nonempty list of `{ "name": "identifier", "status": "PASS" }`.
  Incomplete stages may supply an empty list or `FAIL`/`BLOCKED` checks.
- `repair`: `{ "status": "NOT_NEEDED", "checks": [] }`, or `APPLIED` with
  nonempty identifiers of successful repair/recheck evidence.
  Incomplete stages may use `NOT_RUN` with no repair checks.
- `enforcement`: exactly `mechanism` identifier, `artifact` workspace-relative
  file, `sha256` of that file. This must be actual enforcement/trace evidence,
  distinct from the stage JSON. Its bytes stay in the explicit private workspace.
  Incomplete containment setup may report `enforcement: null`.

The driver validates bindings and artifacts and captures process return code,
output byte counts/digests, evidence hash, role, expected/actual, environment,
checks and repair. It never prints raw process logs. Evidence remains untrusted
runner assertions plus artifacts until independently reviewed. `EXECUTED` means
process evidence was collected; **product acceptance remains UNKNOWN**. A
transcript-shaped file or self-reported isolation is not independent acceptance,
a security audit, legal clearance, or publication authority. Bundle external
and owner gates remain UNKNOWN/PENDING even after native execution.

Offline tests deliberately use synthetic failing Python programs to exercise
preparation, subprocess failure, timeout and recovery. They do not claim genuine
native multi-CLI UAT. Parent-owned actual source export, a reviewed containment
runner and real native product evidence remain separate integration work.
