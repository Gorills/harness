# Performance baselines

Harness keeps wall-clock performance measurements separate from correctness gates. Shared CI
runners are not stable enough for a hard latency threshold, so the required gate records and
checks deterministic work budgets while still printing p50/p95 latency for every run.

Run the representative benchmark with:

```text
make benchmark-hot-paths
```

The default fixture contains 1,000 tracked UTF-8 Python files and performs five warmups followed
by 50 measured iterations. The JSON result includes runtime identity, p50/p95/mean latency, exact
Git command counts, and IPC round trips for:

- model-facing `project_status` through one warmed persistent MCP stdio process and its two daemon
  IPC requests (MCP cold start is intentionally excluded);
- one unchanged Workspace watcher token sample (directory/Git-control metadata plus one rotating
  shard of at most 128 indexed paths);
- one single-path incremental watcher reconciliation;
- one authoritative no-op Structural Index reconciliation.

The repository quality gate runs the same benchmark against a smaller 100-file fixture. It does
not assert elapsed milliseconds. It does assert the recorded structural baseline:

| Path | IPC round trips | Git subprocesses |
| --- | ---: | ---: |
| `project_status` | 2 | 13 |
| idle watcher token | 0 | 0 |
| single-path incremental reconcile | 0 | 6 |
| authoritative no-op scan | 0 | 6 |

The original idle watcher baseline was two Git subprocesses per 0.5-second poll. The current gate
requires zero: idle sampling walks local metadata and escalates to Git confirmation only after a
change. Keep the periodic full reconcile safety net independent from the cheaper watcher hint.

Compare latency only on the same machine, filesystem, Git/Python versions, fixture size, and power
state. Record the complete JSON output with performance work; do not compare a laptop result to a
shared CI runner or turn one noisy sample into a product claim.

## Reference sample

The first representative sample was recorded on 2026-08-31 with Python 3.13.15, Git 2.43.0,
Linux 6.14, an Intel Core i7-4770 (4 cores/8 threads), and an ext4 filesystem on NVMe. The fixture
contained 1,000 tracked files with 5 warmups and 50 measured iterations:

| Path | p50 | p95 | Mean | Structural work |
| --- | ---: | ---: | ---: | --- |
| `project_status` | 33.76 ms | 36.70 ms | 33.85 ms | 2 IPC, 13 Git |
| idle watcher token | 5.95 ms | 7.83 ms | 6.03 ms | 2 Git |
| authoritative no-op scan | 136.26 ms | 165.96 ms | 139.87 ms | 6 Git + 1,000 file reads/hashes |

This sample is evidence for prioritization, not a universal latency promise. In particular, the
watcher currently pays its idle token cost every 0.5 seconds per registered Workspace, while a
triggered or periodic no-op reconciliation exceeds the original 150 ms p95 orientation on this
fixture.

After the watcher optimization on the same machine and fixture, a 50-iteration sample recorded:

| Path | p50 | p95 | Mean | Structural work |
| --- | ---: | ---: | ---: | --- |
| `project_status` | 33.51 ms | 38.89 ms | 34.03 ms | 2 IPC, 13 Git |
| idle watcher token | 5.24 ms | 7.71 ms | 5.40 ms | 0 Git, at most 128 path metadata reads |
| single-path incremental reconcile | 41.97 ms | 60.13 ms | 44.35 ms | 6 Git, 1 file read/hash |
| authoritative no-op scan | 133.27 ms | 155.45 ms | 134.62 ms | 6 Git + 1,000 file reads/hashes |

The idle wall-clock result is similar on this small fixture, but it no longer creates four Git
processes per second per Workspace. A one-file change avoids 999 unrelated reads/hashes and is
about three times faster at p50 than the full reconciliation. Indexed-file rewrites are sampled in
rotating 128-path shards; at the default 0.5-second poll, the worst ordinary detection delay is one
complete shard rotation (about four seconds for this 1,000-file fixture). Directory topology and
Git control metadata are checked every poll, and the periodic full pass remains the convergence
safety net.
