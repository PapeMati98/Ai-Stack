# RAG engineering contract

Return a design or evaluation record with:

- `corpus`: sources, owner, revision/hash, permissions, freshness and exclusions.
- `pipeline`: parsing, chunking, embedding, index, retrieval, rerank, prompt and citation rules.
- `eval_set`: question ids, gold evidence, answer policy, adversarial cases and split.
- `metrics`: recall@k, precision or nDCG, groundedness, answer correctness, abstention,
  latency, cost and error taxonomy.
- `results`: baseline, candidate, confidence or interval, regressions and unresolved gaps.
- `operations`: drift signals, refresh trigger, rollback, access audit and human gate.
- `decision`: keep, revise, blocked or approved-for-separate-deployment-review.

Never treat fluent text as evidence. Preserve source ids and document revisions;
reject unauthorized or untraceable content. Default mode is read/local evaluation.
