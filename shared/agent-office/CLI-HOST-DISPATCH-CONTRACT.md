# B5 native CLI host dispatch: candidate interface v1

The parent owns this interface. Candidate only: do not activate daemon, scheduled task,
owner runtime, keys, IAM or production. Received Office014 and server final2 are frozen.
Use the existing ws-office authenticated peer_host. No extra principal or key.
No invocation may be authorized by Office message body or possession of a dispatch id.

## Binding and eligibility

Future Wake enrollment adds optional execution_mode="host" (legacy default "local"),
execution_host (canonical logical host), executor_agent (exact owner-enrolled Office
address), workspace_flavor="windows"|"posix". Only codex-cli and claude-code-cli qualify.
Native session, exact workspace, supported driver and executable are pinned separately
in a trusted host-local enrollment. Server workspace is typed data: validate with
PureWindowsPath/PurePosixPath, require absolute, reject NUL/control chars and parent
traversal; never exists/resolve/chdir a Windows path on Linux. Do not derive host by
splitting a potentially ambiguous office_agent string.

Server eligibility must re-read current binding/version, owner verification, active
status/expiry, global STOP, allowed trigger/waker, reservation/budget and correlation.
Explicit executor_agent is checked against authenticated caller, execution_host against
forced-command peer_host. Missing host/caller, foreign host/agent, revoked binding or
changed version must refuse before exposing a dispatch or native identifiers.
No authenticated caller may supply another peer_host in RPC arguments.

## Module 1: coordfs/cli_dispatch.py

Class CliDispatches(home, *, now=None, eligibility=None). `eligibility` is a trusted
server-side callback taking the immutable dispatch dict and returning None or a bounded
refusal reason. Without this callback refuse operations; do not default to permissive.
Create state only below the given Office home (wake/cli-dispatches). Retain dispatch
records, no unlink/rmtree. Use the existing checked atomic write/mutex primitives.

enqueue(binding, *, trigger, trigger_ref, correlation_id=None, ttl_s=3600): internal
server operation AFTER current Wake gates, never expose through remote RPC. Closed
binding projection: binding_id, version, execution_host, executor_agent, driver,
native_thread_id, workspace, workspace_flavor. Validate every field, including positive
version and the two explicit supported drivers. ttl_s integer1..3600. trigger one of
message/child-terminal/schedule; references bounded safe text (no control chars).
Dispatch id is SHA256 of binding_id/version/trigger/trigger_ref truncated32hex; changed
payload for the same id refuses conflict, exact repeated enqueue returns the same record.
No message text, arbitrary prompt, binary, argv or environment in any dispatch.

pending(*, peer_host, executor_agent, limit=20): only matching authorized records, with
eligibility checks. limit1..20. Return closed metadata rows; terminal/expired records are
not runnable. Retain terminal records and maintain a bounded active index (max256
active) to avoid rescanning all history. Queue overflow refuses enqueue, never drops data.

claim(dispatch_id, *, peer_host, executor_agent, request_id): authenticated tuple MUST
match binding. request_id exactly32lowerhex, stable per claim. Single pending->claimed
transition under an exclusive lock; create lease_id32hex and claim TTL60s. Same caller
and same request_id replay returns the same immutable claim, marked replayed=true;
different request must refuse. This lease is not standalone authorization: host/agent
checks remain mandatory. Claim response contains normalized immutable target projection,
dispatch/lease/request ids and deadline, not a server command or prompt.

start(dispatch_id, *, peer_host, executor_agent, lease_id, request_id): recheck trusted
eligibility and claim TTL; one claimed->started transition under lock. Return
status=authorized, first_start=true for first successful transition; replay must return
first_start=false and never confer a second launch. Local executor MUST NOT launch on
first_start=false or an ambiguous/lost start response. STOP blocks this transition;
it cannot undo a native process already launched. Stale claims/start-with-lost-reply
are delivery_unknown, not automatically returned to pending. Started records time out
after300s to delivery_unknown, with retained metadata and no new lease.

receipt(dispatch_id, *, peer_host, executor_agent, lease_id, status, exit_code=None):
only the same claim owner; status accepted/rejected/delivery_unknown. Validate optional
bounded integer exit code (no bool). accepted/rejected only for started state. Final
receipts are immutable and idempotent; conflicting results refuse. A valid late receipt
may settle delivery_unknown only for the same original lease, never launch again.
Return a receipt of process delivery, never task-completion/ACCEPT. Store no transcripts.
Global per-store locking is acceptable for these short operations; never hold it while
a native process runs. Filesystem corruption, symlink/reparse state, malformed index or
oversized records fail closed. Index must survive interruption without duplicate launch.
Helpers/errors should use coordfs.CoordError conventions. Keep the module self-contained
except existing safe IO primitives and common validators, with no import of Wake.

## Module 2: coordfs/cli_host.py

HostEnrollment immutable dataclass: binding_id, binding_version, execution_host,
executor_agent, driver, native_thread_id, workspace, executable. All trusted local
settings; require workspace/executable absolute, existing, correct file kind, not
symlink/reparse; no native driver guessing or .py test shim in production runner.
Native ids must satisfy existing safe CLI id rules; no metacharacters/control chars.
Do not load config from server/message nor change registry/ACL automatically.

Class HostExecutor(enrollments, *, journal_home, remote, idle_probe, runner,
now=None, timeout_s=120). `remote` methods pending/claim/start/receipt implement module1
interface and are injected through a trusted adapter. `idle_probe(enrollment)` returns
only true for actual owned-session idle evidence; false or None defers without claim.
Missing proof never defaults idle. Runner accepts argv/cwd/stdin/timeout as keyword
arguments; it returns accepted/rejected/delivery_unknown plus optional exit_code.
Production default runner must construct fixed argv ONLY from trusted enrollment:
codex: executable,exec,resume,--skip-git-repo-check,native_thread_id,-;
claude: executable,--resume,native_thread_id,-p. Prompt always stdin, shell=False,
stdout/stderr discarded, bounded timeout1..180; timeout/interrupt/errors are unknown
or rejected as appropriate, never treated as completion. Tests may inject fake runner;
no real provider calls, arbitrary scripts or network in unit tests.

run_once(): a bounded empty poll contains no model call. Process max20 returned rows,
validate closed metadata and exact matching local enrollment BEFORE claim. Trusted host
and actor are enrollment-owned, never inferred from returned row. Unknown local binding
or any mismatched version/host/driver/native id/workspace refuses without running.
Acquire per-native-session exclusive OS/file fence (including aliases across bindings),
then recheck owned idle evidence. Require unique local dispatch journal with no symlink/
reparse ancestry. Durably fsync a before-launch record before claiming/start. Journals
are append-only, bounded metadata (no prompt/stdout/credentials), retained after finish.
Any retained unfinished/started/unknown dispatch journal prevents rerun after restart;
only retry delivery of the SAME terminal receipt, never retry the process. Concurrent
executors must produce at most one runner call. Keep fence across start and native run.
Recheck idle before start. On first_start=true, persist started intent before invoking
the runner; any failure to fsync means no launch. On start replay/transport timeout,
mark unknown and do not launch. Persist terminal result before reporting receipt;
lost receipt is replayed from that retained result on restart without executing again.
Expiry/STOP/invalid claim/start responses never launch. Retain journal when any RPC fails.
Use no new API, scheduler, credentials, deletion or full-context mailbox polling.
This component is not yet an installed service. It needs actual owned-session idle
qualification and a trusted remote adapter at integration; do not fake those as complete.

## Integration (parent scope after component acceptance)

Wake must route host bindings through dispatch for ALL message/child/schedule branches,
never call _run_driver for them, and attach version/expiry/correlation gates. Transport
closed operations: wake-cli-pending, wake-cli-claim, wake-cli-start, wake-cli-receipt.
Forced-command gate derives peer_host and actor from authenticated context, refuses
peer_host=None for these operations and does not accept host/actor fields in RPC args.
Host service identity is the exact enrolled executor_agent; no new principal. No MCP
tool list change is needed for these unary service operations. Default local Wake and
Desktop relay/self-pull semantics remain unchanged. Windows remote tick remains refused.
The server service units need no extra host flag; binding execution_mode is explicit.
Parent wires the components, trusted host runner/config, candidate entrypoint and tests;
Claude rerenders a NEW package only after the new runtime interface and bytes freeze.
Do not mutate or pretend final2 covers these changes.

## Required verification

Synthetic, isolated tests for wrong host/actor even with known id; invalid Windows and
Posix paths; STOP/revoked/changed/expired binding; duplicate/conflicting enqueue/claim/
start/receipt; two executors; busy and unknown idle; native alias fence; expired lease;
crash/lost start response; lost receipt and restart; corrupt/symlink state; queue bounds;
all triggers and no .py/arbitrary prompt acceptance. Future integration must run actual
synthetic native Codex/Claude CLI sessions on Windows and Linux-server queue UAT.
Unit green alone never proves Desktop wake, end-to-end delivery, 24/7 or production.
