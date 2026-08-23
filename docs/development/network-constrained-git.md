# Network-constrained Git development and publication

This runbook applies when the normal Git remote path is unavailable from the execution environment but repository files and GitHub Git objects can still be read or written through an authenticated API or connector.

It is a fallback, not the preferred development path. When ordinary `git fetch` / `git push` works, use it.

The goal is to preserve the same evidence that a normal Git workflow provides: exact base identity, exact local source bytes, an offline-reproducible quality gate, exact blob identities, one expected Git tree, and a branch ref that moves only after the remote tree matches that expected tree.

## 1. Fresh-confirm the canonical base

Never use a handoff SHA as the only proof of the current base. Fetch the current branch metadata from the repository and record both:

- the canonical base commit SHA;
- the canonical base tree SHA.

If `main` moves before publication, stop and re-evaluate the change against the new base instead of publishing against stale assumptions.

## 2. Bootstrap exact source and toolchain artifacts

Pull-request CI publishes two short-lived artifacts named from the exact PR head SHA:

- `harness-source-<head-sha>` — a `git archive` of the exact tracked PR-head tree;
- `harness-toolchain-<head-sha>` — the pinned `uv` executable plus a dependency cache populated from the locked development environment.

When reusing a previous PR source artifact after that PR has been merged, reuse it only when the extracted source Git tree is exactly equal to the freshly confirmed current base tree. A merge commit SHA may differ while its tree is byte-for-byte identical; tree equality is the relevant source-content proof. For the current repository shape, reconstruct that tree mechanically inside the extracted source root before making changes:

```text
git init
git add --all
git write-tree
```

The resulting tree SHA must equal the freshly confirmed base tree SHA. If repository structure later adds Git links or another archive-unrepresentable tree entry, replace this reconstruction check with an exact Git-tree-aware bootstrap rather than weakening the equality requirement.

When artifact metadata exposes a digest, verify the downloaded artifact against that digest before trusting its bytes. Validate archives before extraction: reject path traversal, unexpected absolute paths, and unexpected archive contents.

For offline execution, keep dependency resolution disabled and put the bundled `uv` directory first in `PATH`. This is required because `scripts/quality.py` invokes child `uv` commands by name; calling the bundled executable for the outer `uv run` is not sufficient if another `uv` appears earlier in `PATH`. A typical loop is:

```text
toolchain_dir=/absolute/path/to/harness-toolchain
cache_dir=/absolute/path/to/extracted-uv-cache
venv_dir=/absolute/path/to/local-venv
export PATH="$toolchain_dir:$PATH"
export UV_PYTHON_DOWNLOADS=never
export UV_CACHE_DIR="$cache_dir"
export UV_PROJECT_ENVIRONMENT="$venv_dir"
"$toolchain_dir/uv" sync --locked --all-groups --offline
"$toolchain_dir/uv" run --frozen --offline python scripts/quality.py
```

The exact extraction paths are environment-specific; the invariant is that the committed lockfile, bundled `uv`, and downloaded cache are sufficient with networking disabled, and every `uv` subprocess in the quality gate resolves to that bundled executable.

## 3. Build one local expected tree

Work in a local Git checkout whose starting tree equals the freshly confirmed canonical base tree. Before publication:

```text
git diff --check
git add --all
git diff --cached --check
git ls-files --stage
git write-tree
```

Treat the resulting `git write-tree` value as the expected feature tree SHA.

For changed regular files and symlinks, use the staged index blob SHA from `git ls-files --stage` as the canonical local blob identity. This is preferable to hashing ad hoc transformed text because the index already reflects repository attributes and Git clean-filter behavior.

Run the focused tests and the full offline quality gate against exactly the bytes represented by this staged tree. If the working tree changes afterward, regenerate the staged evidence and rerun checks proportionate to the change.

## 4. Publish exact Git objects when `git push` is unavailable

If direct Git transport is unavailable, publish through GitHub's Git data/object API or an equivalent authenticated connector. Do not emulate one clean commit with a sequence of Contents API file updates, because each update may create its own commit.

Use this order:

1. Create the feature branch from the exact canonical base commit SHA, or leave the branch ref at that SHA while staging objects.
2. Create one remote blob for each added or modified staged path, preserving the bytes and Git mode represented by the local index.
3. Compare every returned remote blob SHA with the corresponding staged blob SHA from `git ls-files --stage`.
4. If any blob SHA differs, do not reference that blob from a tree. Correct the transport and upload again.
5. Create a remote tree using the canonical base tree plus the exact changed path/mode/blob entries and deletions.
6. Require the returned remote tree SHA to equal the local expected `git write-tree` SHA exactly.
7. Only after the tree matches, create one commit whose parent is the exact canonical base commit and whose tree is that verified feature tree.
8. Move the feature branch ref to that commit using a non-force fast-forward update.
9. Re-fetch the remote branch head and confirm its commit/tree identities before opening the PR.

A blob object uploaded with the wrong bytes is harmless if it remains unreferenced. Never move the branch ref merely because an upload call succeeded.

### Large text payloads

When an API requires base64 for large text payloads, base64-encode the exact file bytes first. If the transport needs line wrapping, insert whitespace only into that single canonical base64 stream at valid 4-character boundaries. Do not manually reconstruct source text across message/tool boundaries.

The returned Git blob SHA is still the final proof: it must equal the staged local blob SHA before the blob may be referenced by the remote tree.

## 5. PR and CI discipline remains unchanged

Fallback publication does not relax review or CI rules:

- open one focused Draft PR for the bounded task;
- state the exact base/head/tree evidence in the PR when fallback publication was used;
- perform adversarial self-review before the first push/branch-ref move that exposes the feature commit;
- use at most the workflow lookup cadence required by the active bounded-workflow instructions;
- never poll an in-progress workflow;
- do not mark a task verified merely because local and remote Git object SHAs matched — tests and CI are separate evidence.

## Failure rule

Fail closed on uncertain identity. If the canonical base, artifact digest, staged blob SHA, expected tree SHA, remote tree SHA, or final branch head cannot be established, stop publication and report the missing evidence instead of guessing.
