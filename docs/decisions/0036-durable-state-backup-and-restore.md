# ADR-0036: Durable-state recovery uses validated SQLite backup archives

- **Status:** Accepted
- **Date:** 2026-08-31
- **Deciders:** Repository architecture baseline

## Context

Harness keeps durable Project, Workspace, Task, checkpoint, event, and Knowledge state in one
SQLite WAL database. SQLite migrations and domain transactions protect normal writes, but the
operator had no supported way to capture or recover that state. Copying only `harness.db` while a
daemon is live can omit committed WAL frames, and replacing a live database behind the owning
daemon violates its singleton and connection-lifetime assumptions.

## Decision

`harness backup ARCHIVE` uses SQLite's online backup API. It may run while the daemon is active and
produces a point-in-time database snapshot that includes committed WAL content. The new archive is
installed without overwriting an existing path and contains exactly:

- `harness.db`, the consistent SQLite snapshot;
- `manifest.json`, with format/schema versions, UTC creation time, database byte size and SHA-256,
  and the creating Harness package version/code fingerprint.

The v1 archive format accepts database snapshots up to 16 GiB so a forged manifest cannot request
unbounded extraction. A larger store needs an explicitly designed streaming/export strategy rather
than silently weakening this local recovery bound.

`harness restore ARCHIVE` is an explicit offline maintenance transition. It cleanly stops the
selected daemon, validates the exact archive entry set, manifest types and bounds, checksum,
SQLite `integrity_check`, foreign keys, contiguous schema history, supported current schema, and
runtime identity before replacing state. An exact-schema archive from another Harness build is
rejected by default; `--allow-runtime-mismatch` is an explicit operator override after compatibility
review. A schema mismatch is never overridden.

Restore takes the same database `flock` as the daemon, so replacement cannot race another owning
process. Before replacement it creates a separate timestamped `.harness-backup` of the current
database. The restored file is prepared in WAL mode in the destination directory, installed with
`0600` permissions, and the parent directory is synced. The daemon remains stopped; the next normal
Harness command starts it against restored state.

The Structural Index and search projections remain included in the database snapshot for exact
recovery, even though they are rebuildable. Restore does not touch repositories or host-owned
configuration.

## Consequences

- Operators have a supported consistent backup and a tested recovery path.
- Live backup does not require pausing normal daemon writes.
- Restore has a short intentional outage and cannot run concurrently with the daemon.
- Strict runtime matching prevents an unreviewed archive/runtime combination; the explicit override
  covers compatible rebuilds without weakening schema validation.
- Archive confidentiality is the operator's responsibility; archives can contain Task and Knowledge
  text and are created current-user-only.

## Verification

Automated tests must prove committed WAL content is captured, an existing archive is not
overwritten, corrupted payloads and schema/runtime mismatches do not mutate the target, the explicit
runtime override remains schema-strict, an existing target gets a usable pre-restore backup, and the
isolated CLI path can perform a backup/restore round trip.
