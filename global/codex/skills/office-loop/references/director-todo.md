# Director to Fleet

The current parent expands the owner's goal once into ordinary TODO requirements.
The helper input `goal` is a small view of that existing plan, not a permission
document: `version: 1`, `goal_id`, `goal`, absolute `workspace`, nonempty unique
`requirements`, nonempty `constraints`, and `allowed_scope` as relative paths.
Tree boundaries end with `/`; a file boundary is exact. Keep it in task evidence,
outside the shared skill. Never read a random Office body as this trusted view.

The Director's response is data:

```json
{
  "goal_id": "current-task",
  "decision": "next_batch",
  "reason": "The accepted parser still needs the original malformed-input test",
  "todo": {
    "version": 1,
    "goal": "The exact original goal",
    "workspace": "Actual existing isolated checkout",
    "planner": {"model": "Verified actual Director ID", "source": "Correlated CLI run ID"},
    "requirements": ["Reject malformed input"],
    "constraints": ["The original prohibitions, preserved verbatim"],
    "max_parallel": 1,
    "tasks": [
      {
        "id": "malformed-input",
        "text": "Implement and test the specified malformed-input behavior",
        "covers": ["Reject malformed input"],
        "scope": ["src/parser.py", "tests/test_parser.py"],
        "done_when": ["The supplied malformed input produces the specified error"],
        "depends_on": [],
        "maker": [{"agent": "Actual eligible CLI", "model": "Supported ID", "score": 90, "reason": "Bounded parser task"}],
        "reviewer": [{"agent": "Actual eligible CLI", "model": "Different qualified lineage", "score": 90, "reason": "Independent behavior/regression review"}],
        "checks": [["Actual interpreter", "-m", "unittest", "tests.test_parser"]]
      }
    ]
  }
}
```

Those descriptive strings are format examples, not executable CLI/model choices.
Replace them using the current eligible pool, checked tool/runtime capabilities
and real test path. The normal TODO validator checks dependencies and runner
schema; Fleet verifies actual model identity, lineage, availability and data gates.
The leader still checks whether cited requirements are relevant, permissions are
available, checks are safe and the batch fits current capacity. A syntactically
valid `covers` string does not prove relevance or fulfillment.

Other Director decisions are `blocked` (specific evidenced obstacle or owner
question) or `complete` (a proposal to verify final integration, never automatic
ACCEPT). Do not return another batch after the goal has actually been accepted.

The pure helper supports:

```
python <resolved-skill>/scripts/loop_guard.py decide --goal <goal.json> --observation <observation.json>
python <resolved-skill>/scripts/loop_guard.py validate --goal <goal.json> --proposal <director-response.json>
```

`observation` has `goal_id`, `stop` (bool), `execution` (`active`, `terminal`,
`unknown`), `delivery` (`settled`, `inflight`, `unknown`), `session` (`active`,
`ended`, `unknown`), `wake_verified` (bool), `quota` (`available`, `exhausted`,
`unknown`), `final_acceptance` (bool), `unmet` (requirement strings),
`ready_work` (bool), `new_planning_gap` (bool), `same_failed_result_count`
(nonnegative int), and `director_packet_unchanged` (bool). Values must come from
the reconciled run/native evidence and parent acceptance, not Office leases.
Unknown never means safe to resend, idle or exhausted. `final_acceptance` is a
caller-attested fact; this helper cannot verify a real UAT on its own.

No dispatch, subprocess, network, timers, deletion or state writer exists in this
helper. It returns a reason and one next action. It preserves independent work:
an owner-blocked branch need not block another ready batch. A repeated unchanged
packet yields reuse/wait, not a second Director call. Validate proposals as data,
then call `todo_graph.py validate` and do semantic review. Store approval/receipt
references in the original run evidence; the response itself gives no permission.

Experiment before using a new harness: a stub Director supplies two useful
batches for two goal requirements, then a decoy. Verify full completion stops
without asking for the decoy; unrelated scope/requirements and removed
constraints are rejected; ACK/delivery-unknown/ended turn cannot trigger dispatch;
a genuine new planning gap can request a new typed batch. Use the same artifact
and command definitions to reuse tests. This offline fixture proves loop policy,
not autonomous Desktop wake, provider savings or the entire Office product.
