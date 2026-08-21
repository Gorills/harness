import sqlite3
from pathlib import Path

import pytest

from harness.storage import connect_database


def test_connect_database_does_not_create_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"

    with pytest.raises(sqlite3.OperationalError):
        connect_database(database)

    assert not database.exists()
