# Linux local release and acceptance

Harness currently targets Linux/POSIX with Claude Code in Normal mode. The package still carries a development version until proprietary-host acceptance is completed.

## Prerequisites

- Linux with a current Git executable.
- Python 3.13.
- `uv` for the repository installation path.
- Claude Code CLI on `PATH` for supported install/uninstall ownership checks.
- SQLite in the selected Python runtime with FTS5.

## Install from this repository

```bash
git clone https://github.com/Gorills/harness.git
cd harness
uv tool install --python 3.13 .
harness install
harness doctor
```

Register and index each Git worktree explicitly:

```bash
cd /path/to/repository
harness scan
harness status
```

`harness scan` also reconciles relevant Harness-owned Claude project skills when the user-scope Harness MCP registration is current. `harness skills list` shows the canonical skill registry without changing projects.

## Upgrade or reinstall

```bash
cd /path/to/harness

git pull
uv tool install --force --python 3.13 .
harness install
harness doctor
```

The second `harness install` is required. It compares the running daemon's frozen schema/version/Python/code identity with the newly installed runtime and cleanly replaces a stale daemon before updating the Claude registration. This covers both a changed virtual-environment path and an in-place reinstall at the same path. Upgrading from the previous Linux release is also explicit: that protocol-v1 daemon does not implement `runtime_diagnostics`, so Harness accepts only its structured unknown-method response, validates legacy `status`, requests the existing clean `shutdown`, then starts and verifies the current runtime. Other IPC/diagnostics failures are not treated as permission to kill or replace a daemon.

## Uninstall

```bash
harness uninstall
```

This removes Harness-owned Claude/project integration while preserving Project Intelligence. To explicitly remove the canonical database and canonical external skill registry as well:

```bash
harness uninstall --purge
```

Purge is fail-closed around canonical filesystem ownership/type checks and database singleton locking. Unknown files outside the explicit Harness data roots are preserved.

## Doctor interpretation

Bare `harness doctor` is read-only and operational. `OK` means the inspected invariant holds, `WARN` means absent/lazy/stale-but-non-destructive state that may need attention, and `FAIL` means an integrity, ownership, compatibility, or runtime mismatch. Any `FAIL` makes the command exit nonzero; warnings alone do not. Project/index/skill checks use one SQLite read snapshot. A quiescent WAL database is opened immutably so doctor does not create `-wal`/`-shm` files merely by inspecting it; an existing live WAL is still read through SQLite's normal read-only WAL path so uncheckpointed durable frames remain visible.

Use `harness doctor --runtime-only` for the old ephemeral SQLite/FTS5 probe and `harness doctor --database PATH` for read-only inspection of one explicitly selected initialized database.

## Automated release gate

`scripts/quality.py` is the repository gate. Its installed-wheel smoke builds and installs the exact wheel, exercises shipping CLI/MCP/daemon behavior, installs the same wheel into a second isolated Python 3.13 environment, verifies stale-daemon replacement, scans a real Git fixture, verifies automatic skill projection, requires a healthy full doctor, checks uninstall preservation, and finally verifies purge.

## Proprietary Claude Code acceptance still required

Automation does not prove vendor UI/runtime behavior. Before calling a specific Claude Code build accepted, verify the matrix in `docs/host-compatibility.md`, including MCP discovery, five tool schemas, `CLAUDE_PROJECT_DIR` Workspace resolution, restart continuity, generated skill visibility/de-duplication, and Harness-owned cleanup. Those checks require the real host and are intentionally not inferred from fake-CLI tests.
