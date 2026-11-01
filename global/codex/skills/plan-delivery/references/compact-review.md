> HISTORYCZNE WYLACZNIE: ten dokument obsluguje przypiete dawne runy/kontrakty i ich recovery. Dla NOWEJ pracy obowiazuje TODO z `../SKILL.md` i globalnego AGENTS.md; nie uruchamiaj opisanych tu fidelity/sufficiency, witness, pieczeci ani bramek planu. Zachowaj historyczne dowody i rzeczywiste granice zgody.

# Compact combined review

`compact` still has two normative gate projections, but it may obtain them from
one review call and one evidence file. The review input deliberately keeps one
array for each ledger dimension; the validator derives the fidelity projection's
empty task list and preserves the complete task list for sufficiency.

```json
{
  "gate": "combined",
  "result": {
    "round": 0,
    "status": "pass",
    "auditor": {
      "identity": "compact-auditor",
      "lineage": "independent-lineage",
      "session_id": "compact-review-session-001",
      "independent_from": ["planner-main"]
    },
    "reviewed_prompt_sha256": "<prompt sha256>",
    "reviewed_plan_sha256": "<planning payload sha256>",
    "reviewed_clause_ids": ["CLAUSE-001"],
    "reviewed_requirement_ids": ["REQ-001"],
    "reviewed_task_ids": ["TASK-001"],
    "attested_hard_risk_flags": [],
    "evidence": {"path": "compact-review.md", "sha256": "<file sha256>"}
  }
}
```

The arrays must cover the complete contract in declared order. `record-review`
accepts `combined` for compact contracts with no hard-risk flags. Legacy `full`
continues to require separate fidelity and sufficiency reviews; only an explicit
`planning.review_policy=full-combined-v1` enables the separate full format,
`full` continues to require separately recorded fidelity and sufficiency
reviews. The two generated gate fields and history rows intentionally reuse the
same auditor, hashes, and evidence path. Outputs remain immutable, so replaying
the command into an existing output or recording another review from the
materialized result is rejected.
