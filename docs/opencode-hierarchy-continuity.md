# OpenCode hierarchy continuity sample

`HardwareIR.system_architecture` remains the canonical topology. This implementation uses the existing hosted connector protocol and requires no connector update or database migration.

## Turn delivery

After authenticating the connector and scoping its session, the polling endpoint loads that owner's latest project revision. It adds the revision ID and serialized hierarchy to the delivered message, together with instructions to read the full project before editing, preserve stable IDs, and update affected systems and interfaces. Stored user messages and conversation history are unchanged. A retry reloads current saved topology.

If the project has no hierarchy, the message asks the agent to author one before detailed implementation. This is agent guidance, not an enforced planning stage. The sample supplies the full tree; branch selection and bounded context retrieval are follow-up work.

## Revision writes

Both `update_project` and `compile_project` use the same reconciliation step before CAD generation and persistence:

- An omitted or null hierarchy preserves a deep copy of the previous tree.
- An explicit tree replaces the prior topology, allowing intentional additions and removals. Keep existing IDs for unchanged systems.
- A legacy design with no tree receives a minimal product root and branches only for explicit electrical/mechanical IR content. The fallback deliberately does not infer firmware or detailed responsibilities. Empty initialization stays empty.
- Blank or duplicate IDs and unresolved interface references return a repairable `invalid_system_architecture` tool error without saving.
- The resulting hierarchy is persisted with the design in the same revision and included in its existing idempotency hash.

For example, a turn requesting a vent receives the existing `mechanical.enclosure` node. The agent can update that node's responsibilities while keeping its ID. An unrelated edit that omits the tree retains the saved topology. Removing a node while leaving an interface pointing to it fails validation.

## Limits

This sample validates structural consistency only. It does not prove that CAD, components, or wiring implement all system responsibilities, enforce stable identity across explicit rewrites, or prevent concurrent stale full-IR writes. It introduces no extra LLM call. Run a live connector/model session separately to evaluate whether the model follows the planning guidance.

## Verification

Run `python -m pytest tests/opencode -q`. Regression coverage includes actual polling delivery, owner-scoped revision reads, preserving stored user text, omitted/null tree preservation, deliberate topology changes, CAD-only fallback behavior, same-revision persistence, and repairable rejection before persistence.
