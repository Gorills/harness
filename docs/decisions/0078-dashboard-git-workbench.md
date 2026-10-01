# ADR-0078: Focused operator Git panel and Task branch prompt

Date: 2026-10-01

Status: Implemented direction requested by the operator

Amends: ADR-0076 and the specification's original Git-client non-goal for this bounded operator surface.

## Context

The operator requested branch visibility, quick checkout and branch creation, branch search,
commits and merges from the dashboard. Hundreds of branches must not become an expanded inventory.
Returning to a Task should make a different recorded branch obvious and offer an explicit switch.

## Decision

Project rows, Project detail and Workspace detail expose compact Git links. An exact folder-scoped
`/workspaces/{id}/git/` page extends the existing Russian dashboard shell and form workflow.
It displays current branch/HEAD, dirty/prepared file counts, five branches with recent commits,
or up to twelve case-insensitive substring matches. Exact matches rank first. Search considers
all local and locally available remote refs, excludes symbolic remote HEAD aliases, and offers
refinement rather than emitting the full inventory. Changed files have twenty-four-row pagination.
On compact screens with changes, the changes/commit panel precedes branches.

Operations are explicit native Git commands in `git_operations`, outside transport handlers:

- Switch to a local branch or create a tracking branch from an explicit remote ref. Branches
  occupied in another worktree cannot be selected for checkout but remain merge sources.
- Create and switch to a branch at current HEAD, preserving dirty files. Native `switch --create`
  is used rather than separately creating a ref before a possibly failed checkout.
- Stage or unstage individual literal paths. Rename unstaging includes both names; copy operations
  preserve independent source changes. Unstaging never removes working files, including on unborn
  HEAD. A prepared file with later working edits is labelled distinctly.
  Explicit bulk buttons stage all project changes with `git add --all -- .` or unstage the entire
  index without changing working files (`reset -- .`, or recursive cached removal on unborn HEAD).
  Bulk operations include every file page and respect Git ignores. They share the same snapshot
  and operation checks as individual actions; unstaging remains unavailable during an operation.
- Commit only the existing index, with an operator message and native identity/signing/hooks.
  Detached HEAD requires creating a branch first. No implicit staging of all files.
- Merge a selected source's captured commit into the displayed current branch, with explicit
  source → destination wording. A clean folder is required before switch or merge; no forced
  checkout, automatic stash or force-push is provided. Conflicts remain native Git state. The
  panel supports staging resolved files, committing the merge and explicit `merge --abort`,
  whose copy explains that merge/resolution edits will be discarded. Other in-progress Git
  operations require terminal completion; staging remains available.

Every POST requires the existing exact Host, same-origin proof, bounded singular form grammar,
the Workspace identity of its Git page and a snapshot token. Snapshot capture repeats worktree
identity, status, HEAD/branch, refs, index and dirty-file metadata before return. Stale state fails
before a command. Shared SQLite `BEGIN IMMEDIATE` serializes dashboard operations against other
dashboard/registry writes; Git retains its own native file/ref locks. The snapshot is an ordinary
metadata freshness proof, not a whole-file content proof or a lock respected by external editors
and CLI Git. Concurrent external commands remain outside this serialization; no global Git
transaction is claimed. Merge uses the sampled source OID even if its ref moves after the proof.

Git effects cannot be rolled back by SQLite. A failed/conflicted/timed-out command can have
changed Git; the response re-reads authoritative state, preserves editable input, invalidates
the Workspace and never automatically retries the POST. In particular a hook timeout can occur
after the commit was created. Subprocesses receive argument arrays, cleaned Git environment,
no interactive stdin/credential prompt, and bounded timeouts. Read output is rejected beyond
2 MiB; error detail is limited to 1800 characters. Invalid UTF-8 POSIX names display escaped
bytes and require native tools for that name instead of submitting lossy paths.

Opening a Task on another branch displays a prompt linking to its exact branch search. Reopening
retains the existing Task revision CAS and this same prompt; Task state and Git are separate
operator decisions. There is no automatic checkout, new Task-branch preference or database
migration. The original Task recorded-branch and applicability contracts remain authoritative.
Git SSE snapshots include index/refs and poll at the existing heartbeat; drafts retain the
existing refresh/submission protection. All operations and search work without JavaScript.

This surface is for human operators, including human Git actions in Hidden Projects. It adds
no MCP capability and does not relax Hidden's prohibition on agent SCM publication. Fetch,
pull/push, branch deletion, history rewriting, diff editing and conflict editing stay in native
Git tools. No network or credential-management subsystem is introduced.

## Verification

Synthetic repositories cover 300 branches and exact search priority, stale state and snapshot
read races, dirty checkout refusal, branch creation with changes, literal pathspecs, prepared
versus working content, rename/copy staging and unstaging, unborn/detached HEAD, occupied
worktrees, remote tracking, merge direction/captured OID, conflict completion and abort.
HTTP tests cover origin/schema/route ownership, POST/303, stale rejection with updated state,
draft messages, Task branch prompt and Git SSE fingerprint alignment. Existing dashboard
HTTP/SSE, Task actions, disclosure, draft and JavaScript regression tests remain in force.
Browser checks use a disposable database/repository with 302 branches, long paths, preparation,
commit and branch search at wide and compact sizes; no registered real Task is mutated for testing.
