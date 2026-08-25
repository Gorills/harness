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

Run focused tests and component checks against exactly the bytes represented by this staged tree, then perform the independent correctness review required by the bounded workflow. When the local full offline quality gate completes reliably, run it here as additional pre-publication evidence.

If the execution wrapper repeatedly interrupts that long gate without a test/check failure, do not spend the task window retrying the same command with cosmetic timeout changes. Use a materially different strategy that covers the same local risk where practical (for example, non-overlapping pytest partitions plus lock/Ruff/mypy/wheel smoke), mark the single-process local full gate as NOT VERIFIED, and proceed to durable task-branch publication only after those replacement checks and review are green. An observed real failure is different: fix it before publication.

The full repository quality gate must still pass in GitHub Actions on the exact PR head before merge. This ordering makes the reviewed candidate durable early enough to survive an execution interruption without weakening the merge gate. If the working tree changes after verification, regenerate the staged evidence and rerun checks proportionate to the change.

## 4. Preflight the publication transport before feature object writes

Do not discover transport limitations halfway through publication. Select and prove one transport before uploading feature bytes:

1. If normal `git fetch` / `git push` works, use normal Git and skip the API fallback entirely.
2. If normal Git transport is unavailable, prefer a machine-side Git Data API path that can read staged Git object bytes directly. In this repository, use `scripts/publish_git_data.py` whenever the execution environment can reach/authenticate to GitHub.
3. If the execution shell cannot reach GitHub but an authenticated connected Git Data tool exposes `create_blob` with `utf-8` encoding, that connector is an allowed transport adapter only when every changed blob decodes as UTF-8. Read the exact staged blob text locally, send it unchanged as raw UTF-8, and require the returned remote blob SHA to equal the staged SHA. Never manually construct or splice base64. If any changed blob is binary/non-UTF-8, stop unless a byte-safe machine transport is available.
4. Before the first **feature** object write, fresh-read the canonical base, prove the local base tree and staged candidate identity, verify task-branch state, enumerate every changed object SHA/size, and prove the chosen write path with the constant unreferenced probe `harness exact-tree publication preflight\n`. Its Git blob SHA must be `aa0e051fdac1e0590943347f86e2650dcd63fa9e`. The repo-owned `preflight` action performs these checks automatically; a connector adapter must establish the same evidence explicitly.
5. If authentication, network/write access, local base identity, branch state, candidate identity, UTF-8 eligibility for connector mode, or the write probe cannot be established, stop here. Do not start a partial feature upload and do not switch encoding mid-publication.

Example:

```text
export GH_TOKEN=...  # or GITHUB_TOKEN; never print it
python scripts/publish_git_data.py preflight \
  --repo Gorills/harness \
  --branch feat/example
```

The fallback publisher uses only the Python standard library plus local `git`; it does not add a runtime dependency. Fine-grained GitHub credentials need repository Contents write permission for Git Data mutations. The probe intentionally tests that permission before feature objects are sent.

## 5. Publish one exact staged tree

After focused/component verification and independent correctness review are green, publish the same staged tree:

```text
python scripts/publish_git_data.py publish \
  --repo Gorills/harness \
  --branch feat/example \
  --message "feat: example"
```

The publisher performs this fail-closed sequence:

1. Fresh-read the canonical base commit/tree and rebuild the candidate from the local Git index.
2. Read every staged blob with `git cat-file blob <sha>` and independently recompute its Git blob SHA.
3. Base64-encode those bytes **inside the publisher process** and send them to GitHub's Git Data `create blob` endpoint. The file contents are never manually copied, chunked, reassembled, or base64-spliced across an agent/tool boundary.
4. Require every returned remote blob SHA to equal the staged blob SHA. A mismatch stops before tree/commit/ref publication.
5. Create the remote tree from the canonical base tree and the exact changed path/mode/object entries. Require the returned tree SHA to equal local `git write-tree`.
6. Fresh-read `main` again after the unreferenced object writes. If it moved, stop before creating a durable task ref.
7. Create one unreferenced commit whose tree is the verified feature tree and whose sole parent is the exact canonical base commit; re-read and verify both fields, then fresh-read the canonical base once more before any task-ref change.
8. If the task branch does not exist, create it at the exact base commit. If it exists, require that it still points exactly at the base.
9. Move the task branch to the feature commit with a non-force update. If a create/update-ref request returns an ambiguous transport failure, re-read the ref: treat the mutation as successful only when the remote SHA already equals the exact expected SHA; otherwise fail closed.
10. Re-fetch the branch and commit; require final commit/tree/parent identity to match the verified values. A repeated `publish` is idempotent when the task branch already points to a commit with the exact candidate tree and exact canonical-base parent, so an execution interruption immediately after a successful ref update can be resumed safely without publishing another commit.

Git blob/tree/commit creation is content-addressed and safe to retry: failed or mismatched objects remain unreferenced. The branch ref is the durability boundary and moves only after the complete tree and parentage are proven.

### Why manual base64 is forbidden

GitHub's Git Data API supports `utf-8` and `base64` blob payloads, but the encoding itself is not the difficult part. The failure mode is transporting a large hand-built encoded string through a conversational/tool boundary: one missing, duplicated, or altered character produces different bytes and therefore a different Git blob SHA. The SHA check detects that corruption, but only after wasting a publication attempt.

Prefer machine code that reads and encodes the exact staged bytes. A connected Git Data tool may also publish a staged blob directly as raw UTF-8 when that blob is valid UTF-8 and the returned Git SHA exactly matches the staged SHA. This is not manual reconstruction: the exact text is sent once with `encoding=utf-8`, and SHA identity is the acceptance condition. If the changed blob is binary/non-UTF-8, or the connector cannot preserve the complete text payload, classify publication as unavailable during preflight rather than attempting chunk reconstruction.

Do not emulate the final feature commit with a sequence of Contents API updates. That produces intermediate commits and makes exact one-commit/tree evidence harder to reason about.

## 6. PR and CI discipline remains unchanged

Fallback publication does not relax review or CI rules:

- open one focused Draft PR for the bounded task;
- state the exact base/head/tree evidence in the PR when fallback publication was used;
- perform the independent correctness review before the first push/branch-ref move that exposes the feature commit;
- once the reviewed tree has enough focused/component verification to be safe to publish, prioritize making that exact tree durable as the task commit/PR instead of consuming the remaining execution window on repeated local full-suite retries;
- use at most the workflow lookup cadence required by the active bounded-workflow instructions;
- never poll an in-progress workflow;
- require the complete repository quality gate to succeed on the exact PR head before Ready/merge;
- do not mark a task verified merely because local and remote Git object SHAs matched — tests and CI are separate evidence.

## Failure rule

Fail closed on uncertain identity. If the canonical base, artifact digest, staged blob SHA, expected tree SHA, remote tree SHA, or final branch head cannot be established, stop publication and report the missing evidence instead of guessing.
