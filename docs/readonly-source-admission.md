# Read-only source admission

The Fleet controller can admit an exact, read-only Python source file whose
filename contains the generic word `secret`. This exception applies only to
the declared input file. It does not authorize writes, broaden the writable
scope, or override other protected-path rules.

## Admission and execution

The trusted parent supplies the programmatic dispatch and the exact read-only
input. A prompt or task JSON alone cannot grant access. In `advisory` and
`review` modes, `scope` defines read scope and need not be empty. A reviewer
envelope contains `goal`, a string `task`, list-valued `scope`, `inputs` and
`done_when`, plus `constraints` and `feedback`; it does not contain `checks`.
The bridge may add reviewer evidence fields.

For example, these are corresponding values supplied by the trusted parent:

```json
{"goal":"Review", "task":"Review the input", "scope":["docs"],
 "inputs":["src/example_secret.py"], "done_when":["Report findings"],
 "constraints":[], "feedback":null}
```

```json
{"scope":["docs"], "inputs":["src/example_secret.py"], "checks":[]}
```

The first object is the reviewer envelope; the second is the programmatic
dispatch, whose keys are exactly `scope`, `inputs` and `checks`. Their scope
and inputs must match, and the reviewer dispatch has no checks. Implementation
mode retains its mandatory declared checks.

The controller performs its normal required-input preflight in the actual
execution workspace. On success, it creates an internal verified-input
capability and uses it to bind the final policy. Missing or failed preflight,
missing matching records, and a preview-only policy cannot reach inference.
Preview is for structural validation and is never active at execution.

The policy is kept outside the model workspace and is bound to the trusted
launch by its SHA-256 digest. The admitted read is restricted to the verified
path, workspace, run and mode. File identity and content must remain the same:
reads and snapshots use the checked bytes, and drift or an incomplete snapshot
is refused. The source is checked with the packaged canonical scanner; a
filename exception is not an exception for sensitive content.

Hard protected names and directories, environment and authentication files,
credential and key material, path traversal, links and hard links remain
refused. An input cannot overlap writable scope. Directory admission is not a
substitute for an exact source-file input.

## Boundary and verification

This is an application tool boundary, not an operating-system sandbox. It
does not grant host-shell access or expand operating-system permissions. Native
Vibe uses the pinned Fleet tool integration. Declared Python checks, when
available for the applicable mode, run in the pinned offline Docker environment;
read-only review does not need checks.

The local guarded run recorded 117 tests with 5 skipped and no failures or
errors. Native SDK coverage was skipped and no provider activity occurred.
This is local evidence, not production validation or native user acceptance.
A narrow native read-only canary passed; global activation remains pending.
