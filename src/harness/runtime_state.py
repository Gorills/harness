from __future__ import annotations

import os
import stat
from pathlib import Path

from harness.runtime_paths import RuntimePaths


class RuntimeStateError(RuntimeError):
    """Raised when canonical Harness persisted state cannot be trusted safely."""


def canonical_database_purge_candidates(paths: RuntimePaths) -> tuple[Path, ...]:
    """Return known SQLite data/sidecar files owned by the canonical Harness database."""
    return (
        paths.database,
        paths.database.with_name(f"{paths.database.name}-wal"),
        paths.database.with_name(f"{paths.database.name}-shm"),
        paths.database.with_name(f"{paths.database.name}-journal"),
    )


def canonical_database_lock_path(paths: RuntimePaths) -> Path:
    """Return the process-lifetime singleton lock path for the canonical Harness database."""
    return paths.database.with_name(f"{paths.database.name}.lock")


def preflight_canonical_database_state(paths: RuntimePaths) -> None:
    """Fail closed on unsafe canonical state-directory or SQLite artifact identities."""
    state_directory = paths.database.parent
    try:
        directory_metadata = state_directory.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeStateError("Harness state directory could not be inspected") from exc
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_ISLNK(directory_metadata.st_mode)
        or directory_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(directory_metadata.st_mode) & 0o077
    ):
        raise RuntimeStateError("Harness refused an unsafe state directory")

    for candidate in (
        *canonical_database_purge_candidates(paths),
        canonical_database_lock_path(paths),
    ):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeStateError("Harness database state could not be inspected") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeStateError("Harness refused unsafe database state")
