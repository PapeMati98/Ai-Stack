# Agent Graph integration

Site tools do not require a new executor or provider. Use the existing graph
schema and represent them as a semantic verification boundary.

## Recommended topology

```text
implementation/integration
  -> deterministic-tests
  -> site-tools-semantic-journey (parent-local, user-journey-qa)
  -> remote-vm-human-journey (parent-local, user-journey-qa/visual-qa)
  -> fleet-quality-gate
  -> fleet-security-gate
  -> parent-final-acceptance
```

The semantic node uses `executor: parent-local`, `workspace_mode: read-only`,
`capability: user-journey-qa`, and `evidence_kind: browser-e2e`. Its evidence
must name the page, tool, runtime/model, authenticated test role, Site tools
discovery, invocation result, Sources/Recently used record, and independently
verified application state.

Do not add this node as a mandatory graph dependency from registration-code or
mock evidence alone. First prove native discovery and one invocation in a fresh
supported desktop task. Diagnostic output may inform a human or an explicit
later policy node, but it has no implicit authority over Fleet routing or
approval decisions.

The remote VM node remains mandatory when a human-visible surface changed. It
tests the ordinary UI in Chrome and returns visual evidence. It must not reuse
the Site-tools result as proof that the visual journey passed.

If Site tools are unavailable because of runtime, rollout, model, workspace,
or a closed tab, record that exact blocker. Do not emulate discovery with DOM
clicks and do not downgrade the independent browser journey. The graph may
continue without the optional semantic node only when Site tools were not part
of the delivery contract; otherwise amend or block it explicitly.

Write-capable or consequential Site-tool execution does not become a new
authority. Use an existing `human` approval node where global policy requires
owner approval, then execute and verify the exact approved effect.
