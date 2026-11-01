# Operations playbook contract

Return a Markdown or JSON playbook with:

- `purpose_and_scope`: trigger, outcome, in-scope and excluded actions.
- `roles`: accountable owner, responsible operator, consulted and informed parties.
- `prerequisites`: access, inputs, health checks, approvals and maintenance window.
- `procedure`: numbered action, expected result, evidence, stop condition and owner.
- `controls`: least privilege, secrets boundary, rate limits, logging and change record.
- `recovery`: failure modes, rollback, restore point, escalation and communication draft.
- `verification`: health, functional, security and acceptance checks with evidence paths.
- `status`: draft, reviewed, blocked or approved-for-execution, plus review date.

Separate read, draft and act. Do not include live credentials or imply authorization.
Destructive, production, external-message and irreversible steps must be explicit
approval gates with a tested rollback or a recorded reason why rollback is impossible.
