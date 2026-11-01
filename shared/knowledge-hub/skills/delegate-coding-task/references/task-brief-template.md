# Task brief template (mandatory for every delegation)

Executors do not think — they execute. The brief must let a mid-tier model
finish without inventing decisions. Write it to `.delegations/<task>.brief.md`
in the workdir and pass the file to `scripts/delegate.sh`.

```markdown
# TASK: <one-line goal>

## CONTEXT
- Project dir: <absolute path>
- Key files: <paths the executor must read first>
- Background: <3-6 bullets max; what it needs, nothing more>

## STEPS
1. <exact step, files/paths named>
2. <exact step>
3. ...

## CONSTRAINTS
- Allowed: <dirs, commands, file types>
- Forbidden: <e.g. no dependency upgrades, no force-push, no touching X>
- Secrets: none needed / <how provided safely>

## DONE CRITERIA (verifiable)
- [ ] <command that must exit 0, e.g. `npm test`>
- [ ] <grep/file existence check>
- [ ] <behavioral check>

## OUTPUT CONTRACT
- Write a short report to <path>: what changed, files touched, checks run, results.
- Do not paste the whole diff into chat; list paths.

## BUDGET
- Max ~<N> agent turns / <M> minutes. If blocked: stop, report blocker, do not improvise.
```

## Brief-writing rules

- One task per brief. Split epics into per-tool briefs.
- Numbers over adjectives: "extract lines 40-120 of X into Y" not "clean up X".
- Verification commands must be runnable by the orchestrator afterwards —
  you will re-run them to verify the executor's self-report.
- If the task needs judgment beyond the brief, it is mis-routed: escalate the
  model tier or keep the task on the orchestrator.
