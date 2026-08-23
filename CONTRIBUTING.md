# Contributing to Harness

Harness is intentionally developed in small, reviewable slices because it sits between several fast-changing agent hosts and a durable local state model.

## Change workflow

1. Choose one bounded task with a concrete acceptance condition.
2. Read the relevant specification, audit, architecture, ADRs, implementation, and tests.
3. Make the smallest complete change.
4. Add or update tests that can fail for the behavior being changed.
5. Run relevant local checks.
6. Review the complete diff for regressions, accidental disclosure, host coupling, and undocumented contract changes.
7. Open a focused PR that states what is verified and what is not.

Do not combine unrelated cleanup with feature or bug work.

## Local quality checks

Harness development uses `uv 0.12.5` with Python 3.13 and the committed `uv.lock`:

```text
uv sync --locked --all-groups
uv run --frozen python scripts/quality.py
```

The quality gate checks lock freshness, Ruff formatting/lint, strict mypy, pytest, and an isolated wheel-install smoke test for the `harness` and `harnessd` console scripts.

For checkout-local CLI/daemon work that must not share state with a system Harness install:

```text
scripts/dev sync
scripts/dev harness doctor
scripts/dev harness scan
scripts/dev stop
```

See [`docs/development/isolated-development.md`](docs/development/isolated-development.md). Prefer `scripts/dev` over a global `harness` on `PATH`.

If direct Git/network access is unavailable but repository Git objects remain accessible through an authenticated API or connector, follow [`docs/development/network-constrained-git.md`](docs/development/network-constrained-git.md). The fallback preserves exact base/tree identity, uses the PR source/toolchain artifacts for offline work, verifies staged and remote blob/tree SHAs, and moves the feature branch away from the verified base only after the complete remote tree matches the locally verified expected tree.

## Branches and pull requests

- Never develop directly on `main` once the initial repository bootstrap exists.
- Prefer short-lived branches named by intent, for example `feat/task-store` or `fix/mcp-budget`.
- Keep commits reviewable; avoid generated noise.
- A PR changing MCP-visible fields, database schema, host integration behavior, or architecture must identify the contract affected.
- Do not merge with known failing relevant checks.

## Architecture changes

Create or amend an ADR when a change:

- changes a core process or ownership boundary;
- adds a durable extension interface;
- changes task/session/workspace semantics;
- changes persistence or migration strategy;
- changes MCP contract semantics;
- introduces a mandatory external service or new deployment component;
- changes how host-specific behavior is isolated.

Implementation detail that preserves an existing decision usually does not need an ADR.

## Compatibility policy

Host adapters target documented public behavior, not guessed internal behavior. If an official host document does not establish a needed behavior, mark it as requiring real-host acceptance instead of encoding the assumption as fact.

MCP protocol compatibility must be handled through the official SDK. Harness must not implement its own production protocol stack.

## Security and privacy

- Local-only is the default trust boundary.
- Never log raw source, full model context, secrets, or credential-bearing host configuration by default.
- Never treat host-provided client metadata as an authentication primitive.
- External embedding or LLM providers require explicit opt-in and a documented data boundary.
- Integration cleanup removes only Harness-owned artifacts.

## Definition of verified

A statement is **verified** only when the relevant check actually ran against the current change. Documentation review, static reasoning, unit tests, wire tests, and real-host acceptance are distinct levels of evidence and must not be conflated.
