# Domain Docs

Before exploring the codebase, read:

- `CONTEXT.md` at the repository root.
- `CONTEXT-MAP.md` if it exists.
- Relevant ADRs under `docs/adr/`.

If these files do not exist, proceed silently. Domain-modeling skills create them lazily when terminology or architectural decisions are resolved.

## File structure

This is a single-context repository:

/
├── CONTEXT.md
├── docs/adr/
└── src/

Use terminology defined in `CONTEXT.md`. If proposed work contradicts an existing ADR, identify the conflict explicitly instead of silently overriding it.
