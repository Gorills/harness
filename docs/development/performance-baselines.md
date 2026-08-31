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
- one unchanged Workspace watcher token sample;
- one authoritative no-op Structural Index reconciliation.

The repository quality gate runs the same benchmark against a smaller 100-file fixture. It does
not assert elapsed milliseconds. It does assert the recorded structural baseline:

| Path | IPC round trips | Git subprocesses |
| --- | ---: | ---: |
| `project_status` | 2 | 13 |
| idle watcher token | 0 | 2 |
| authoritative no-op scan | 0 | 6 |

These counts are deliberately a baseline, not a desirable target. An optimization must update the
budget only after its regression tests prove the same correctness contract. Keep the periodic full
reconcile safety net independent from any cheaper watcher hint.

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
