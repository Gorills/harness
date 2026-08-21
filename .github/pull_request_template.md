## Scope

<!-- One bounded task. State the exact behavior/contract changed. -->

## Why

<!-- Link issue/ADR/spec section when relevant. -->

## Verification

### VERIFIED

- <!-- Checks actually run and their results. -->

### NOT VERIFIED

- <!-- Host/platform/integration behavior not exercised. -->

### ASSUMPTIONS

- <!-- Any remaining assumption that matters. -->

### BLOCKERS

- <!-- None, or concrete blockers. -->

## Risk review

- [ ] No unrelated refactor or cleanup is mixed in.
- [ ] Model-visible MCP contract/budgets were updated and tested if affected.
- [ ] Negative-disclosure coverage was considered if context/output changed.
- [ ] Database migration/recovery was considered if persistence changed.
- [ ] Host-specific behavior remains isolated in adapters.
- [ ] Documentation/ADR was updated if an architectural or user-visible contract changed.
