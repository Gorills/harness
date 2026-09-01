# ADR-0018: Reconcile registered Workspace indexes through daemon-owned change hints

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Repository architecture baseline

## Context

The Structural Index is derived state: Git and the Workspace filesystem remain authoritative, and `scan_workspace` already provides the deterministic transactional reconciliation path. The architecture requires incremental watcher behavior to debounce/coalesce changes, treat watcher events only as hints, and converge again after missed events or daemon downtime.

Introducing a second event-driven index writer would duplicate reconciliation rules and make rename, ignore-policy, crash-recovery, and source-race behavior harder to prove. The first watcher slice also needs to remain usable from the locked/offline installation without adding a platform-specific watcher dependency before its cross-platform behavior is proven.

A daemon restart creates another correctness boundary. Existing registered Workspaces may have changed while `harnessd` was not running, and a newly registered Workspace can change immediately after its explicit `harness scan` but before the background watcher has discovered it. Both cases must converge without relying on a later user command.

## Decision

On the currently supported POSIX daemon path, `harnessd` owns one background Workspace watcher thread with its own SQLite connection. The watcher never writes `indexed_files` from an event payload. It only decides when the existing authoritative `scan_workspace` reconciliation should run.

The first implementation uses a bounded standard-library polling hint rather than a new mandatory filesystem-watcher dependency. For each registered Workspace, the hint hashes:

- Git porcelain status, including untracked paths;
- current Git `HEAD` identity so clean checkout/reset changes are observable;
- lightweight filesystem identity for currently dirty paths and `.harnessignore`.

Watcher Git probes set `GIT_OPTIONAL_LOCKS=0` so sampling does not intentionally refresh the Git index. Hint sampling has a finite deadline and stores no source contents; Harness uses only Git output plus filesystem metadata as the watcher token. Content hashes persisted in the Structural Index are still produced only by the existing deterministic scan when reconciliation is required.

Changes are debounced/coalesced. Manual daemon scans and watcher scans share one process-local scan lock, so the daemon never runs those two reconciliation paths concurrently. A successful manual scan enqueues an explicit Workspace invalidation before its IPC response is sent; this closes the discovery race where the Workspace changes after registration/scan but before the watcher has established its first sample.

Every Workspace discovered after watcher startup is scheduled for an initial authoritative reconciliation after the debounce window. This repairs changes made while the daemon was stopped. In addition, a slower periodic full reconciliation is retained as a safety net for hints that cannot represent an external change or were otherwise missed. A watcher poll performs at most one full-scan attempt and checks daemon shutdown between Workspace operations, so shutdown does not multiply the scan deadline by the number of registered Workspaces.

If a scan observes another hint change across the scan, the Workspace remains pending and is reconciled again after debounce. Transient sampling/index/Git failures leave the Workspace pending and retry later. The daemon signals the watcher to stop and joins it before releasing the selected database/socket ownership locks, so an old watcher cannot continue writing after daemon ownership has been relinquished. An unexpected watcher-thread termination is a daemon error rather than silently claiming continuous reconciliation.

This ADR defines the correctness contract, not the permanent event-source implementation. A future proven native watcher may replace polling while preserving the same hint-only, debounced, authoritative-rescan and restart-recovery semantics.

## Consequences

### Positive

- Filesystem/Git remain the only source of truth; incremental behavior reuses one transactional index reconciliation implementation.
- Registered Workspace indexes converge automatically after ordinary create/modify/delete changes, clean Git HEAD moves, missed hints, and daemon downtime.
- Manual scans and background scans cannot race each other inside one daemon process.
- The first slice adds no package dependency, schema migration, IPC method, MCP field, or model-visible contract.
- A future native watcher can be substituted without changing the daemon/index ownership boundary.

### Costs and limits

- Polling Git state consumes bounded local subprocess work for every registered Workspace while the daemon runs.
- The hint is intentionally incomplete; periodic full reconciliation is still required for convergence after unobservable changes.
- Background reconciliation is currently implemented only on the existing POSIX daemon transport path. Windows daemon IPC/watching remains a separate unsupported platform slice.
- This slice still does not add language/symbol parsing, FTS population, semantic search, or dashboard watcher controls.

## Verification

Automated tests must prove:

- dirty tracked rewrites change the hint even when porcelain status shape is unchanged;
- clean tracked-tree changes caused by a Git HEAD move trigger reconciliation;
- symlinked-parent escape during hint inspection fails closed;
- rapid changes debounce into an authoritative scan of final filesystem state;
- manual-scan lock contention does not lose a pending watcher reconciliation;
- transient scan failure keeps the change pending and retries;
- a scan whose pre-scan token is unavailable is conservatively reconciled again;
- daemon shutdown stops before beginning additional Workspace work;
- periodic reconciliation repairs a deliberately missed hint;
- an existing Workspace changed while the daemon was stopped reconciles after watcher startup;
- a Workspace changed immediately after its first daemon scan converges without another explicit scan;
- real daemon create/modify/delete changes converge while existing IPC status remains usable.

## 2026-08-31 amendment: subprocess-free idle hints and bounded path reconciliation

The original polling hint launched `git status` plus `git rev-parse HEAD` every 0.5 seconds for
every registered Workspace. A measured 1,000-file fixture showed that this cost was small per call
but permanently repeated while idle, while the periodic no-op full scan crossed the original
150 ms p95 orientation. The correctness contract does not require Git subprocesses while no
observable metadata changes.

The watcher now uses two levels of hints:

1. Every ordinary poll computes a subprocess-free metadata token from one rotating shard of at
   most 128 known directory identities, bounded Git control metadata (`HEAD`, index, refs, packed
   refs, and local exclude state), and one rotating shard of at most 128 indexed path identities.
   It does not hash source contents, does not restat the full directory inventory, and
   does not traverse Git objects or known generated roots. One ordinary poll samples at most
   two established Workspaces for those idle hints and rotates through the registry, so idle
   cost stays bounded as registered Workspace count grows. Unreadable directories (EACCES/EPERM)
   are omitted from descent rather than aborting the hint; a single permission-denied path must not
   force a full-scan retry loop. New/deleted paths change their parent
   directory metadata; an existing-file rewrite is detected within one shard rotation.
2. Only a changed/unknown metadata token runs the existing Git status/HEAD confirmation. If HEAD
   is unchanged and the union of previous/current dirty paths is bounded to 256 paths, the watcher
   reconciles those paths through the same Git ignore/default-exclude, stable-read, transactional
   index/FTS, and Knowledge-staleness rules. `.harnessignore` changes always force a full scan.
   If both the previous and current Git snapshots stay above that 256-path bound with HEAD
   unchanged, the watcher does not immediately full-scan again; it waits for the five-minute
   periodic safety pass. Persistent oversized dirty trees (untracked media, scratch) must not
   hash the whole Workspace on every metadata blip.

Initial/restart reconciliation, manual invalidation, Workspace relocation, Git HEAD changes,
unknown/oversized path sets, sampling failures, and a Git-confirmation pass every five minutes
remain the safety net. The five-minute pass no longer force-hashes every indexed file when Git
status is unchanged; an oversized dirty tree is full-scanned at most once per five-minute window
unless HEAD moved. A post-reconcile metadata and Git sample retains the existing
changed-during-scan retry rule, except a still-oversized dirty set with unchanged HEAD does not
immediately queue another full scan. Metadata can still miss deliberately restored inode timestamps,
so a later Git HEAD move or the next allowed full pass remains required for those rare cases.

The structural performance gate therefore changes the idle budget from two Git subprocesses per
poll to zero. It does not impose a wall-clock threshold on shared CI hardware.
