# Context Governance

Forma treats a project as a set of typed, versioned namespaces. A namespace is
owned by one domain agent, while cross-domain context is a separate governed
handoff.

## Contract

`forma_core.workspaces.projects.context_governance` applies a versioned,
deny-by-default policy:

```text
project object
  -> source namespace
  -> recipient namespace
  -> explicit attribute and field allowlist
  -> context projection + audit receipt
```

An agent may always read its own namespace. A different agent receives nothing
unless a `ContextShareRule` names both namespaces and the fields that may cross
the boundary. Inline data and credential-like fields are sanitized even when a
rule allows the enclosing attribute.

The projection receipt records `allowed_sources`, `denied_sources`, omitted
attributes, the policy schema version, and the recipient. This makes a handoff
inspectable without including the denied data in the prompt.

## Initial Handoffs

- Product overview shares intent, requirements, and constraints with domain specialists.
- Architecture shares the system hierarchy with domain specialists.
- Electrical shares component summaries with mechanical and BOM agents, interface assignments with firmware, and build-level connectivity with assembly.
- Electrical shares its complete state only with validation.
- Mechanical shares the canonical mechanical state with visuals, validation, and assembly.
- Validation shares safety gates with assembly.
- BOM and assembly share their approved views with documentation.
- Project metadata and history are not cross-domain context by default.

This is intentionally an allowlist, not a promise that every domain should see
the whole project. For example, mechanical placement can use a component's
identity and envelope purpose without receiving electrical pin definitions.

## Relationship To Issues

Issue `#391` needs canonical mechanical objects upstream of CAD. Issue `#424`
needs images, placements, and CAD to refer to the same mechanical state. Domain
governance is the boundary that prevents each artifact agent from inventing or
silently copying facts owned by another domain:

```text
intent -> typed project namespaces -> governed handoffs -> artifacts
```

The policy does not replace the mechanical model or image-reference
decomposition. It gives those future objects a safe sharing contract.
